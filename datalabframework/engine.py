import sys
import os, time
import shutil
import textwrap

from datalabframework import logging
from datalabframework import elastic

from datalabframework import resource
from datalabframework.yaml import YamlDict
from datalabframework._utils import to_ordered_dict, find, python_version, get_hadoop_version_from_system, get_tool_home, run_command, str_join

import pandas as pd
from datalabframework.spark import dataframe

from timeit import default_timer as timer

import pyspark.sql.functions as F
import pyspark.sql.types as T

# purpose of engines
# abstract engine init, data read and data write
# and move this information to metadata

# it does not make the code fully engine agnostic though.

import pyspark

def get(name, md, rootdir):
    engine = NoEngine()

    if md['engine']['type'] == 'spark':
        engine = SparkEngine(name, md, rootdir)

    return engine

class Engine:
    def __init__(self, name):
        self._name = name
        self._config = {}
        self._info = {}
        self._ctx = None
        self._type = None
        self._version = None
        self._timezone = None

    def config(self):
        keys = [
            'type',
            'name',
            'version',
            'info',
            'config',
            'env',
            'timezone'
        ]
        d = {
            'type': self._type,
            'name': self._name,
            'version': self._version,
            'info': self._info,
            'config': self._config,
            'env': self.env(),
            'timezone': self._timezone
        }
        return YamlDict(to_ordered_dict(d, keys))

    def env(self):
        return {}

    def context(self):
        return self._ctx

    def load(self, path=None, provider=None, format=None, schema=None, username=None, password=None, **options):
        raise NotImplementedError

    def save(self, obj, path=None, provider=None, format=None, mode=None, schema=None, username=None, password=None, **options):
        raise NotImplementedError

    def copy(self, md_src, md_trg, mode='changelog'):
        raise NotImplementedError

    def list(self, provider, path):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


class NoEngine(Engine):
    def __init__(self):
        self._type = 'none'
        self._version = 0

        super().__init__('no-compute-engine', {}, os.getcwd())

    def load(self, path=None, provider=None, **kargs):
        raise ValueError('No engine loaded.')

    def save(self, obj, path=None, provider=None, **kargs):
        raise ValueError('No engine loaded.')

    def copy(self, md_src, md_trg, mode='append'):
        raise ValueError('No engine loaded.')

    def list(self, provider):
        raise ValueError('No engine loaded.')

    def stop(self):
        pass


class SparkEngine(Engine):
    def set_info(self):
        hadoop_version = None
        hadoop_detect_from = None
        try:
            session = pyspark.sql.SparkSession.builder.getOrCreate()
            hadoop_version = session.sparkContext._gateway.jvm.org.apache.hadoop.util.VersionInfo.getVersion()
            hadoop_detect_from = 'spark'
            self.stop(session)
        except Exception as e:
            print(e)
            pass
        
        if hadoop_version is None:
            hadoop_version = get_hadoop_version_from_system()
            hadoop_detect_from = 'system'
        
        if hadoop_version is None:
            logging.warning('Could not find a valid hadoop install.')

        hadoop_home = get_tool_home('hadoop', 'HADOOP_HOME', 'bin')[0]
        spark_home = get_tool_home('spark-submit', 'SPARK_HOME', 'bin')[0]
        
        spark_dist_classpath = os.environ.get('SPARK_DIST_CLASSPATH')
        spark_dist_classpath_source = 'env'
        
        if not spark_dist_classpath:
            spark_dist_classpath_source = os.path.join(spark_home,'conf/spark-env.sh')
            if os.path.isfile(spark_dist_classpath_source):
                with open(spark_dist_classpath_source) as s:
                    for line in s:
                        pattern = 'SPARK_DIST_CLASSPATH='
                        pos = line.find(pattern)
                        if pos>=0:
                            spark_dist_classpath = line[pos+len(pattern):].strip()
                            spark_dist_classpath = run_command(f'echo {spark_dist_classpath}')[0]

        if hadoop_detect_from == 'system' and (not spark_dist_classpath):
            logging.warning(textwrap.dedent("""
                    SPARK_DIST_CLASSPATH not defined and spark installed without hadoop
                    define SPARK_DIST_CLASSPATH in $SPARK_HOME/conf/spark-env.sh as follows:
                       
                       export SPARK_DIST_CLASSPATH=$(hadoop classpath)
                    
                    for more info refer to: 
                    https://spark.apache.org/docs/latest/hadoop-provided.html
                """))
        
        self._info['python_version']=python_version()
        self._info['hadoop_version']=hadoop_version
        self._info['hadoop_detect']=hadoop_detect_from
        self._info['hadoop_home']=hadoop_home
        self._info['spark_home']=spark_home
        self._info['spark_classpath']=spark_dist_classpath.split(':') if spark_dist_classpath else None
        self._info['spark_classpath_source']=spark_dist_classpath_source
        
        return 
    
    def get_detected_submit_lists(self, detect=True):
        
        submit_types = ['jars', 'packages', 'py-files', 'repositories']
        
        submit_objs=dict()
        for submit_type in submit_types:
            submit_objs[submit_type] = []

        if not detect:
            return submit_objs

        # get hadoop, and configured metadata services
        hadoop_version = self._info['hadoop_version']

        providers = self._metadata['providers']
        services = {v['service'] for v in providers.values()}
        services = sorted(list(services))

        #### submit: jars
        jars = submit_objs['jars']
            
        if 'oracle' in services:
            jar  = 'http://www.datanucleus.org/downloads/maven2/'
            jar += 'oracle/ojdbc6/11.2.0.3/ojdbc6-11.2.0.3.jar'
            jars.append(jar)
        
        #### submit: packages
        packages = submit_objs['packages']
        
        for v in services:
            if v == 'mysql':
                packages.append('mysql:mysql-connector-java:8.0.12')
            elif v == 'sqlite':
                packages.append('org.xerial:sqlite-jdbc:3.25.2')
            elif v == 'postgres':
                packages.append('org.postgresql:postgresql:42.2.5')
            elif v == 'mssql':
                packages.append('com.microsoft.sqlserver:mssql-jdbc:6.4.0.jre8')
            elif v == 'minio':
                if hadoop_version:
                    packages.append(f"org.apache.hadoop:hadoop-aws:{hadoop_version}")
                else:
                    logging.warning('Hadoop is not detected. '
                                    'Could not load hadoop-aws package ')
        
        #### submit: py-files
        pyfiles = submit_objs['py-files']
        
        #### print debug
        
        for submit_type in submit_types:
            if submit_objs[submit_type]:
                print(f'Loading detected {submit_type}:')
                for i in submit_objs[submit_type]:
                    print(f'  -  {i}')

        return submit_objs

    def get_metadata_submit_lists(self):
                
        submit_types = ['jars', 'packages', 'py-files']
        
        submit_objs=dict()
        md_submit = self._metadata['engine']['submit']
        for submit_type in submit_types:
            submit_objs[submit_type] =  md_submit[submit_type]
        
        #### print debug
        
        for submit_type in submit_types:
            if submit_objs[submit_type]:
                print(f'Loading {submit_type}:')
                for i in submit_objs[submit_type]:
                    print(f'  -  {i}')

        return submit_objs

    def set_submit_args(self, detected_objs, metadata_objs):
        
        submit_types = ['jars', 'packages', 'py-files']
        
        submit_args = ''

        for submit_type in submit_types:
            # collects lists
            m_list = metadata_objs[submit_type]
            d_list = detected_objs[submit_type]
            
            # build args
            submit_list =  d_list + m_list
            if submit_list:
                objs = ','.join(submit_list)
                submit_args += ' ' if submit_args else ''
                submit_args += f'--{submit_type} {objs}'
        
        # set PYSPARK_SUBMIT_ARGS env variable
        submit_args = '{} pyspark-shell'.format(submit_args)
        os.environ['PYSPARK_SUBMIT_ARGS'] = submit_args
    
    def set_env_variables(self, detect=True):
        for e in ['PYSPARK_PYTHON','PYSPARK_DRIVER_PYTHON']:
            if sys.executable and not os.environ.get(e):
                os.environ[e] = sys.executable

    def set_conf_detect(self, conf, detect=True):
        # setting for minio
        if detect:
            for v in self._metadata.get('providers', {}).values():
                if v['service'] == 'minio':
                    url = 'http://{}:{}'.format(v['hostname'], v.get('port', 9000))
                    s3a = "org.apache.hadoop.fs.s3a.S3AFileSystem"

                    conf.set("spark.hadoop.fs.s3a.endpoint", url) \
                        .set("spark.hadoop.fs.s3a.access.key", v.get('username')) \
                        .set("spark.hadoop.fs.s3a.secret.key", v.get('password')) \
                        .set("spark.hadoop.fs.s3a.impl", s3a) \
                        .set("spark.hadoop.fs.s3a.path.style.access", "true")
                    break

            # if timezone set to 'naive', 
            # force UTC to override local system and spark defaults 
            # This will effectively avoid any  conversion of datetime object to/from spark
            if self._timezone == 'naive':
                self._timezone = 'UTC'

            if self._timezone:
                timezone = self._timezone
                os.environ['TZ'] = timezone
                time.tzset()
                conf.set('spark.sql.session.timeZone', timezone)
                conf.set('spark.driver.extraJavaOptions', f'-Duser.timezone={timezone}')
                conf.set('spark.executor.extraJavaOptions', f'-Duser.timezone={timezone}')
            else:
                # use spark and system defaults
                pass

    def set_conf_kv(self, conf):
        # appname
        if self._metadata['engine']['jobname']:
            logging.warning('deprecated: metadata engine/jobname is generated')
        conf.setAppName(self._name)

        # set master
        master_url = self._metadata['engine']['master']
        conf.setMaster(master_url)

        # set kv conf from metadata
        conf_md = self._metadata['engine']['config']

        for k, v in conf_md.items():
            if isinstance(v, (bool, int, float, str)):
                conf.set(k, v)

    def initialize_spark_sql_context(self,spark_session, spark_context):
        try:
            del pyspark.sql.SQLContext._instantiatedContext
        except:
            pass
        
        if spark_context is None:
            spark_context = spark_session.sparkContext
        
        pyspark.sql.SQLContext._instantiatedContext = None
        sql_ctx = pyspark.sql.SQLContext(spark_context, spark_session)
        return sql_ctx

    def start_context(self, conf):
        try:
            # init the spark session
            session = pyspark.sql.SparkSession.builder.config(conf=conf).getOrCreate()
            
            # fix SQLContext for back compatibility
            self.initialize_spark_sql_context(session,session.sparkContext)

            # pyspark set log level method
            # (this will not suppress WARN before starting the context)
            session.sparkContext.setLogLevel("ERROR")
            return session
        except Exception as e:
            logging.error('Could not start the engine context')
            return None
        
    def env(self):
        
        vars=[
            'SPARK_HOME',
            'HADOOP_HOME',
            'JAVA_HOME',
            'PYSPARK_PYTHON', 
            'PYSPARK_DRIVER_PYTHON',
            'PYTHONPATH',
            'PYSPARK_SUBMIT_ARGS',
            'SPARK_DIST_CLASSPATH',
        ]
        
        return {v:os.environ.get(v) for v in vars}
    
    def __init__(self, name, md, rootdir):
        super().__init__(name, md, rootdir)
        
        # set engine type
        self._type = 'spark'

        # timezone
        self._timezone = self._metadata['engine']['timezone']
        
        # set engine info
        self.set_info()
        
        # print statement
        print(f'Init engine "{self._type}"')
        
        # auto detect set to True 
        # will load configuration properties and packages
        detect = self._metadata['engine']['submit']['detect']
        
        # set submit args via env variable
        detected_objs = self.get_detected_submit_lists(detect)
        metadata_objs = self.get_metadata_submit_lists()
        self.set_submit_args(detected_objs, metadata_objs)
        
        # set other spark-related environment variables
        self.set_env_variables()

        # set spark conf object
        print(f"Connecting to spark master: {self._metadata['engine']['master']}")

        conf = pyspark.SparkConf()
        self.set_conf_detect(conf, detect)
        self.set_conf_kv(conf)

        # stop current session before creating a new one
        self.stop()
        
        # start spark
        spark_session = self.start_context(conf)
        
        # record the data in the engine object for debug and future references
        self._config = dict(conf.getAll())
                    
        if spark_session:
            self._config = dict(dict(spark_session.sparkContext.getConf().getAll()))
            
            # set version if spark is loaded
            self._version = spark_session.version
            print(f'Engine context {self._type}:{self._version} successfully started')

            # store the spark session
            self._ctx = spark_session

    def stop(self, spark_session=None):
        try:
            spark_session = spark_session or self._ctx
            sc = None
            if spark_session:
                sc = spark_session.sparkContext
                spark_session.stop()
                
            cls = pyspark.SparkContext
            sc = sc or cls._active_spark_context
            
            if sc:
                sc.stop()
                sc._gateway.shutdown()
                
            cls._active_spark_context = None
            cls._gateway = None
            cls._jvm = None
        except Exception as e:
            print(e)
            logging.warning('Could not fully stop the engine context')
            
    def load(self, path=None, provider=None, catch_exception=True, **kargs):
        if isinstance(path, str):
            md = resource.metadata(self._rootdir, self._metadata, path, provider)
        elif isinstance(path, dict):
            md = path

        core_start = timer()
        obj = self.load_dataframe(md, catch_exception, **kargs)
        core_end = timer()
        if obj is None:
            return obj

        prep_start = timer()
        date_column = '_date' if md['date_partition'] else md['date_column']
        obj = dataframe.filter_by_date(
            obj,
            date_column,
            md['date_start'],
            md['date_end'],
            md['date_window'])

        # partition and sorting (hmmm, needed?)
        if date_column and date_column in obj.columns:
            obj = obj.repartition(date_column)

        if '_updated' in obj.columns:
            obj = obj.sortWithinPartitions(F.desc('_updated'))

        num_rows = obj.count()
        num_cols = len(obj.columns)

        obj = dataframe.cache(obj, md['cache'])

        prep_end = timer()

        log_data = {
            'md': md,
            'mode': kargs.get('mode', md.get('options', {}).get('mode')),
            'records': num_rows,
            'columns': num_cols,
            'time': prep_end - core_start,
            'time_core': core_end - core_start,
            'time_prep': prep_end - prep_start
        }
        logging.info(log_data) if obj is not None else logging.error(log_data)

        obj.__name__ = path
        return obj

    def load_with_pandas(self, kargs):
        logging.warning("Fallback dataframe reader")
        
        #conversion of *some* pyspark arguments to pandas
        kargs.pop('inferSchema', None)
        
        kargs['header'] = 'infer' if kargs.get('header') else None
        kargs['prefix'] = '_c'
        
        return kargs
        
    def load_dataframe(self, md, catch_exception=True, **kargs):
        obj = None
        options = md['options']

        try:
            if md['service'] in ['local', 'file']:
                if md['format'] == 'csv':
                    try:
                        obj = self._ctx.read.options(**options).csv(md['url'], **kargs)
                    except:
                        kargs = self.load_with_pandas(kargs)
                    
                    if obj is None:
                        df = pd.read_csv(md['url'], **kargs)
                        obj = self._ctx.createDataFrame(df)

                elif md['format'] == 'json':
                    try:
                        obj = self._ctx.read.options(**options).json(md['url'], **kargs)
                    except:
                        kargs = self.load_with_pandas(kargs)
                    
                    if obj is None:
                        df = pd.read_json(md['url'])
                        obj = self._ctx.createDataFrame(df)

                elif md['format'] == 'jsonl':
                    try:
                        obj = self._ctx.read.option('multiLine', True) \
                                       .options(**options).json(md['url'], **kargs)
                    except:
                        kargs = self.load_with_pandas(kargs)
                    
                    if obj is None:
                        df = pd.read_json(md['url'], lines=True)
                        obj = self._ctx.createDataFrame(df)

                elif md['format'] == 'parquet':
                    try:
                        obj = self._ctx.read.options(**options).parquet(md['url'], **kargs)
                    except:
                        kargs = self.load_with_pandas(kargs)
                    
                    if obj is None:
                        df = pd.read_parquet(md['url'])
                        obj = self._ctx.createDataFrame(df)
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return None

            elif md['service'] in ['hdfs', 'minio', 's3a']:
                if md['format'] == 'csv':
                    obj = self._ctx.read.options(**options).csv(md['url'], **kargs)
                elif md['format'] == 'json':
                    obj = self._ctx.read.options(**options).json(md['url'], **kargs)
                elif md['format'] == 'jsonl':
                    obj = self._ctx.read.option('multiLine', True)\
                                   .options(**options).json(md['url'], **kargs)
                elif md['format'] == 'parquet':
                    obj = self._ctx.read.options(**options).parquet(md['url'], **kargs)
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return None

            elif md['service'] in ['sqlite', 'mysql', 'postgres', 'mssql', 'oracle']:

                obj = self._ctx.read \
                    .format('jdbc') \
                    .option('url', md['url']) \
                    .option("dbtable", md['table']) \
                    .option("driver", md['driver']) \
                    .option("user", md['username']) \
                    .option('password', md['password']) \
                    .options(**options)

                # load the data from jdbc
                obj = obj.load(**kargs)

            elif md['service'] == 'elastic':
                results = elastic.read(md['url'], options.get('query', {}))
                rows = [pyspark.sql.Row(**r) for r in results]
                obj = self.context().createDataFrame(rows)
            else:
                logging.error({'md': md, 'error_msg': f'Unknown service "{md["service"]}"'})
        except Exception as e:
            if catch_exception:
                print(e.message)
                logging.error({'md': md, 'error': str(e.message)})
                return None
            else:
                raise e

        return obj

    def save(self, obj, path=None, provider=None, **kargs):

        if path is None:
            path  = obj.__name__
            logging.warning(f'No path provider, using {path}')
                               
        if isinstance(path, str):
            md = resource.metadata(self._rootdir, self._metadata, path, provider)
        elif isinstance(path, dict):
            md = path

        prep_start = timer()
        options = md['options'] or {}
        
        if md['date_partition'] and md['date_column']:
            tzone = 'UTC' if self._timestamps == 'naive' else self._timezone
            obj = dataframe.add_datetime_columns(obj, column=md['date_column'], tzone=tzone)
            kargs['partitionBy'] = ['_date'] + kargs.get('partitionBy', options.get('partitionBy', []))

        if md['update_column']:
            obj = dataframe.add_update_column(obj, tzone=self._timezone)

        if md['hash_column']:
            obj = dataframe.add_hash_column(obj, cols=md['hash_column'],
                                            exclude_cols=['_date', '_datetime', '_updated', '_hash', '_state'])

        date_column = '_date' if md['date_partition'] else md['date_column']
        obj = dataframe.filter_by_date(
            obj,
            date_column,
            md['date_start'],
            md['date_end'],
            md['date_window'])

        obj = dataframe.cache(obj, md['cache'])

        num_rows = obj.count()
        num_cols = len(obj.columns)

        # force 1 file per partition, just before saving
        obj = obj.repartition(1, *kargs['partitionBy']) if kargs.get('partitionBy') else obj.repartition(1)
        # obj = obj.coalesce(1)

        prep_end = timer()

        core_start = timer()
        result = self.save_dataframe(obj, md, **kargs)
        core_end = timer()

        log_data = {
            'md': dict(md),
            'mode': kargs.get('mode', options.get('mode')),
            'records': num_rows,
            'columns': num_cols,
            'time': core_end - prep_start,
            'time_core': core_end - core_start,
            'time_prep': prep_end - prep_start
        }

        logging.info(log_data) if result else logging.error(log_data)

        return result

    def is_spark_local(self):
        return self._config.get('spark.master').startswith('local')
                               
    def save_with_pandas(self, md, kargs):
        if not self.is_spark_local():
            logging.warning("Fallback dataframe writer")
        
        if os.path.exists(md['url']) and os.path.isdir(md['url']):
            shutil.rmtree(md['url'])
                               
        #conversion of *some* pyspark arguments to pandas
        if md['format'] == 'csv':
            kargs.pop('mode', None)
            kargs['index'] = False
            
            if kargs.get('header') is None :
                kargs['header'] = False 

        return kargs
                           
    def directory_to_file(self, path, ext):
        if os.path.exists(path) and os.path.isfile(path):
            return
        
        dirname = os.path.dirname(path)
        basename = os.path.basename(path)
        
                               
        filename = list(filter(lambda x: x.endswith(ext), os.listdir(path)))
        if len(filename)!=1:
            logging.warning('cannot convert if more than a partition present')
            return
        else:
            filename = filename[0]
                               
        shutil.move(os.path.join(path,filename), dirname)
        if os.path.exists(path) and os.path.isdir(path):
            shutil.rmtree(path)
                               
        shutil.move(os.path.join(dirname,filename), os.path.join(dirname,basename))
        return
        
    def save_dataframe(self, obj, md, **kargs):

        options = md['options'] or {}
        try:
            if md['service'] in ['local', 'file']:
                if md['format'] == 'csv':
                    if self.is_spark_local():
                        obj.coalesce(1).write.options(**options).csv(md['url'], **kargs)
                        self.directory_to_file(md['url'], 'csv')
                    else:
                        kargs = self.save_with_pandas(md, kargs)
                        obj.toPandas().to_csv(md['url'], **kargs)
                        
                elif md['format'] == 'json':
                    if self.is_spark_local():
                        obj.coalesce(1).write.options(**options).json(md['url'], **kargs)
                        self.directory_to_file(md['url'], 'json')
                    else:
                        self.save_with_pandas(md, kargs)
                        obj.toPandas().to_json(md['url'])
                               
                elif md['format'] == 'jsonl':
                    if self.is_spark_local():
                        obj.coalesce(1).write.options(**options)\
                                 .option('multiLine', True)\
                                 .json(md['url'], **kargs)
                        self.directory_to_file(md['url'], 'json')
                    else:
                        self.save_with_pandas(md, kargs)
                        obj.toPandas().to_json(md['url'], orient='records', lines=True)
                elif md['format'] == 'parquet':
                    if self.is_spark_local():
                        obj.coalesce(1).write.options(**options).parquet(md['url'], **kargs)
                    else:
                        self.save_with_pandas(md, kargs)
                        obj.toPandas().to_parquet(md['url'])
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return False

            elif md['service'] in ['hdfs', 'minio', 's3a']:
                if md['format'] == 'csv':
                    obj.write.options(**options).csv(md['url'], **kargs)
                elif md['format'] == 'json':
                    obj.write.options(**options).json(md['url'], **kargs)
                elif md['format'] == 'jsonl':
                    obj.write.options(**options)\
                             .option('multiLine', True)\
                             .json(md['url'], **kargs)
                elif md['format'] == 'parquet':
                    obj.write.options(**options).parquet(md['url'], **kargs)
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return False

            elif md['service'] in ['sqlite', 'mysql', 'postgres', 'mssql', 'oracle']:
                obj.write \
                    .format('jdbc') \
                    .option('url', md['url']) \
                    .option("dbtable", md['table']) \
                    .option("driver", md['driver']) \
                    .option("user", md['username']) \
                    .option('password', md['password']) \
                    .options(**options) \
                    .save(**kargs)

            elif md['service'] == 'elastic':
                mode = kargs.get("mode", None)
                obj = [row.asDict() for row in obj.collect()]
                elastic.write(obj, md['url'], mode, md['resource_path'], options['settings'], options['mappings'])
            else:
                logging.error({'md': md, 'error_msg': f'Unknown service "{md["service"]}"'})
                return False
        except Exception as e:
            logging.error({'md': md, 'error_msg': str(e)})
            raise e

        return True
                               
    def copy(self, md_src, md_trg, mode='append'):
        # timer
        timer_start = timer()

        # src dataframe
        df_src = self.load(md_src)
                               
        # if not path on target, get it from src
        if not md_trg['resource_path']:
           md_trg = resource.metadata(
               self._rootdir, 
               self._metadata, 
               md_src['resource_path'], 
               md_trg['provider_alias'])

        # logging
        log_data = {
            'src_hash': md_src['hash'],
            'src_path': md_src['resource_path'],
            'trg_hash': md_trg['hash'],
            'trg_path': md_trg['resource_path'],
            'mode': mode,
            'updated': False,
            'records_read': 0,
            'records_add': 0,
            'records_del': 0,
            'columns': 0,
            'time': timer() - timer_start
        }

        # could not read source, log error and return
        if df_src is None:
            logging.error(log_data)
            return

        num_rows = df_src.count()
        num_cols = len(df_src.columns)

        # empty source, log notice and return
        if num_rows == 0 and mode == 'append':
            log_data['time'] = timer() - timer_start
            logging.notice(log_data)
            return

        # overwrite target, save, log notice/error and return
        if mode == 'overwrite':
            if md_trg['state_column']:
                df_src = df_src.withColumn('_state', F.lit(0))

            result = self.save(df_src, md_trg, mode=mode)

            log_data['time'] = timer() - timer_start
            log_data['records_read'] = num_rows
            log_data['records_add'] = num_rows
            log_data['columns'] = num_cols

            logging.notice(log_data) if result else logging.error(log_data)
            return

        # trg dataframe (if exists)
        try:
            df_trg = self.load(md_trg, catch_exception=False)
        except:
            df_trg = dataframe.empty(df_src)

        # de-dup (exclude the _updated column)

        # create a view from the extracted log
        df_trg = dataframe.view(df_trg)
                               
        # capture added records
        df_add = dataframe.diff(df_src, df_trg, ['_date', '_datetime', '_updated', '_hash', '_state'])
        rows_add = df_add.count()

        # capture deleted records
        rows_del = 0
        if md_trg['state_column']:
            df_del = dataframe.diff(df_trg, df_src, ['_date', '_datetime', '_updated', '_hash', '_state'])
            rows_del = df_del.count()

        updated = (rows_add + rows_del) > 0

        num_cols = len(df_add.columns)
        num_rows = max(df_src.count(), df_trg.count())

        # save diff
        if updated:
            if md_trg['state_column']:
                df_add = df_add.withColumn('_state', F.lit(0))
                df_del = df_del.withColumn('_state', F.lit(1))

                df = df_add.union(df_del)
            else:
                df = df_add

            result = self.save(df, md_trg, mode=mode)
        else:
            result = True

        log_data.update({
            'updated': updated,
            'records_read': num_rows,
            'records_add': rows_add,
            'records_del': rows_del,
            'columns': num_cols,
            'time': timer() - timer_start
        })

        logging.notice(log_data) if result else logging.error(log_data)

    def list(self, provider, path=''):

        df_schema = T.StructType([
                T.StructField('name',T.StringType(),True),
                T.StructField('type',T.StringType(),True)])

        df_empty = self._ctx.createDataFrame(data=(), schema=df_schema)
                      
        if isinstance(provider, str):
            md = resource.metadata(self._rootdir, self._metadata, None, provider)
        elif isinstance(provider, dict):
            md = provider
        else:
            logging.warning(f'{str(provider)} cannot be used to reference a provider')
            return df_empty

        try:
            if md['service'] in ['local', 'file']:
                lst = []
                rootpath = os.path.join(md['provider_path'], path)
                for f in os.listdir(rootpath):
                    fullpath = os.path.join(rootpath, f)
                    if os.path.isfile(fullpath):
                        obj_type='FILE'
                    elif os.path.isdir(fullpath):
                        obj_type='DIRECTORY'
                    elif os.path.islink(fullpath):
                        obj_type='LINK'
                    elif os.path.ismount(fullpath):
                        obj_type='MOUNT'
                    else:
                        obj_type='UNDEFINED'
                
                    obj_name = f
                    lst += [(obj_name, obj_type)]
                
                if lst:
                    df = self._ctx.createDataFrame(lst, ['name', 'type'])
                else:
                    df = df_empty
                
                return df
                               
            elif md['service'] in ['hdfs', 'minio', 's3a']:
                sc = self._ctx._sc
                URI = sc._gateway.jvm.java.net.URI
                Path = sc._gateway.jvm.org.apache.hadoop.fs.Path
                FileSystem = sc._gateway.jvm.org.apache.hadoop.fs.FileSystem
                fs = FileSystem.get(URI(md['url']), sc._jsc.hadoopConfiguration())

                provider_path = md['provider_path'] if  md['service']=='hdfs' else '/'
                obj = fs.listStatus(Path(os.path.join(provider_path, path)))
                
                lst = []
                
                for i in range(len(obj)):
                    if obj[i].isFile():
                        obj_type='FILE'
                    elif obj[i].isDirectory():
                        obj_type='DIRECTORY'
                    else:
                        obj_type='UNDEFINED'
                
                    obj_name = obj[i].getPath().getName()
                    lst += [(obj_name, obj_type)]

                if lst:
                    df = self._ctx.createDataFrame(lst, ['name', 'type'])
                else:
                    df = df_empty
                
                return df
            
            elif md['format'] == 'jdbc':
                # remove options from database, if any
                database = md["database"].split('?')[0]
                schema = md['schema']
                if md['service'] == 'mssql':
                    query = f"""
                        ( SELECT table_name, table_type 
                          FROM INFORMATION_SCHEMA.TABLES 
                          WHERE table_schema='{schema}'
                        ) as query
                        """
                elif md['service'] == 'oracle':
                    query = f"""
                        ( SELECT table_name, table_type 
                         FROM all_tables 
                         WHERE table_schema='{schema}'
                        ) as query
                        """
                elif md['service'] == 'mysql':
                    query = f"""
                        ( SELECT table_name, table_type 
                          FROM information_schema.tables 
                          WHERE table_schema='{schema}'
                        ) as query
                        """
                elif md['service'] == 'postgres':
                    query = f"""
                        ( SELECT table_name, table_type
                          FROM information_schema.tables 
                          WHERE table_schema = '{schema}'
                        ) as query
                        """
                else:
                    # vanilla query ... for other databases
                    query = f"""
                            ( SELECT table_name, table_type 
                              FROM information_schema.tables'
                            ) as query
                            """

                obj = self._ctx.read \
                    .format('jdbc') \
                    .option('url', md['url']) \
                    .option("dbtable", query) \
                    .option("driver", md['driver']) \
                    .option("user", md['username']) \
                    .option('password', md['password']) \
                    .load()

                # load the data from jdbc
                lst = []
                for x in obj.select('TABLE_NAME', 'TABLE_TYPE').collect():
                    lst.append((x.TABLE_NAME, x.TABLE_TYPE))
                               
                if lst:
                    df = self._ctx.createDataFrame(lst, ['name', 'type'])
                else:
                    df = df_empty
                
                return df

            else:
                logging.error({'md': md, 'error_msg': f'List resource on service "{md["service"]}" not implemented'})
                return  df_empty
        except Exception as e:
            logging.error({'md': md, 'error_msg': str(e)})
            raise e

        return  df_empty
