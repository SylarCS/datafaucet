import os, time, copy

from datalabframework import logging
from datalabframework import elastic

from datalabframework.metadata.resource import get_metadata
from datalabframework._utils import YamlDict, to_ordered_dict

import pandas as pd
from datalabframework.spark import dataframe

from timeit import default_timer as timer

import pyspark.sql.functions as F
import pyspark.sql.types as T

from datetime import datetime

# purpose of engines
# abstract engine init, data read and data write
# and move this information to metadata

# it does not make the code fully engine agnostic though.

import pyspark


class Engine:
    def __init__(self, name, md, rootdir):
        self._name = name
        self._metadata = md
        self._rootdir = rootdir
        self._config = None
        self._env = None
        self._ctx = None
        self._type = None
        self._version = None
        self._timezone = md.get('engine', {}).get('timezone')
        self._timestamps = md.get('engine', {}).get('timestamps')

    def config(self):
        keys = [
            'type',
            'name',
            'version',
            'config',
            'env',
            'rootdir',
            'timezone'
        ]
        d = {
            'type': self._type,
            'name': self._name,
            'version': self._version,
            'config': self._config,
            'env': self._env,
            'rootdir': self._rootdir,
            'timezone': self._timezone,
            'timestamps': self._timestamps
        }
        return YamlDict(to_ordered_dict(d, keys))

    def context(self):
        return self._ctx

    def load(self, path=None, provider=None, **kargs):
        raise NotImplementedError

    def save(self, obj, path=None, provider=None, **kargs):
        raise NotImplementedError

    def copy(self, md_src, md_trg, mode='append'):
        raise NotImplementedError

    def list(self, provider):
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
    def set_submit_args(self):

        submit_args = ''
        submit_md = self._metadata.get('engine', {}).get('submit', {})

        #### submit: jars
        items = submit_md.get('jars')
        jars = items if items else []

        if jars:
            submit_jars = ' '.join(jars)
            submit_args = '{} --jars {}'.format(submit_args, submit_jars)

        #### submit: packages
        items = submit_md.get('packages')
        packages = items if items else []

        for v in self._metadata.get('providers', {}).values():
            if v['service'] == 'mysql':
                packages.append('mysql:mysql-connector-java:8.0.12')
            elif v['service'] == 'sqlite':
                packages.append('org.xerial:sqlite-jdbc:jar:3.25.2')
            elif v['service'] == 'postgres':
                packages.append('org.postgresql:postgresql:42.2.5')
            elif v['service'] == 'mssql':
                packages.append('com.microsoft.sqlserver:mssql-jdbc:6.4.0.jre8')
            elif v['service'] == 'mongodb':
                packages.append('org.mongodb.spark:mongo-spark-connector_2.11:2.2.0')

        if packages:
            submit_packages = ','.join(packages)
            submit_args = '{} --packages {}'.format(submit_args, submit_packages)

        #### submit: py-files
        items = submit_md.get('py-files')
        pyfiles = items if items else []

        if pyfiles:
            submit_pyfiles = ','.join(pyfiles)
            submit_args = '{} --py-files {}'.format(submit_args, submit_pyfiles)

        # set PYSPARK_SUBMIT_ARGS env variable
        submit_args = '{} pyspark-shell'.format(submit_args)
        os.environ['PYSPARK_SUBMIT_ARGS'] = submit_args

    def set_context_args(self, conf):
        # jobname
        app_name = self._metadata.get('engine', {}).get('jobname', self._name)
        conf.setAppName(app_name)

        # set master
        conf.setMaster(self._metadata.get('engine', {}).get('master', 'local[*]'))

    def set_conf_kv(self, conf):
        conf_md = self._metadata.get('engine', {}).get('config', {})

        # setting for minio
        for v in self._metadata.get('providers', {}).values():
            if v['service'] == 'minio':
                conf.set("spark.hadoop.fs.s3a.endpoint", 'http://{}:{}'.format(v['hostname'], v.get('port', 9000))) \
                    .set("spark.hadoop.fs.s3a.access.key", v['access']) \
                    .set("spark.hadoop.fs.s3a.secret.key", v['secret']) \
                    .set("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
                    .set("spark.hadoop.fs.s3a.path.style.access", True)
                break

        # if timezone is not set, engine treats timestamps as 'naive' 
        if self._timestamps == 'naive':
            os.environ['TZ'] = 'UTC'
            time.tzset()
            conf.set('spark.sql.session.timeZone', 'UTC')
            conf.set('spark.driver.extraJavaOptions', '-Duser.timezone=UTC')
            conf.set('spark.executor.extraJavaOptions', '-Duser.timezone=UTC')
        elif self._timezone:
            timezone = self._timezone
            os.environ['TZ'] = timezone
            time.tzset()
            conf.set('spark.sql.session.timeZone', timezone)
            conf.set('spark.driver.extraJavaOptions', f'-Duser.timezone={timezone}')
            conf.set('spark.executor.extraJavaOptions', f'-Duser.timezone={timezone}')
        else:
            # use spark and system defaults
            pass

        for k, v in conf_md.items():
            if isinstance(v, (bool, int, float, str)):
                conf.set(k, v)

    def __init__(self, name, md, rootdir):
        super().__init__(name, md, rootdir)

        # set submit args via env variable
        self.set_submit_args()

        # set spark conf object
        conf = pyspark.SparkConf()
        self.set_context_args(conf)
        self.set_conf_kv(conf)

        # stop current session before creating a new one
        pyspark.SparkContext.getOrCreate().stop()

        # set log level fro spark
        sc = pyspark.SparkContext(conf=conf)

        # pyspark set log level method
        # (this will not suppress WARN before starting the context)
        sc.setLogLevel("ERROR")

        # record the data in the engine object for debug and future references
        self._config = dict(sc._conf.getAll())
        self._env = {'PYSPARK_SUBMIT_ARGS': os.environ['PYSPARK_SUBMIT_ARGS']}

        self._type = 'spark'
        self._version = sc.version

        # store the sql context
        self._ctx = pyspark.SQLContext(sc)

    def stop(self):
        pyspark.SparkContext.getOrCreate().stop()

    def find_version(self, date=None, md=None):
        try:
            versions = [x.name for x in self.list(md,path=md['resource_path']).select('name').collect()]
        except:
            return None
        
        versions = [version.split('=')[1] for version in versions if '_version=' in version]
        versions.sort(reverse=True)

        if date == None:
            return versions[0]
        else:
            for version in versions:
                if datetime.strptime(version, '%Y-%m-%d-%H-%M-%S') <= date:
                    return version
        
        return None

    def load_cdc(self, path=None, provider=None, date=None, catch_exception=True, **kargs):
        """
        Load the cdc database at date. If date is null, the latest one will be loaded

        :param date: the query datetime object
        """
        
        if isinstance(path, YamlDict):
            md = path.to_dict()
        elif isinstance(path, str):
            md = get_metadata(self._rootdir, self._metadata, path, provider)
        elif isinstance(path, dict):
            md = path

        version = self.find_version(date, md)
        md_copy = copy.deepcopy(md)
        md_copy['url'] += '/_version=' + version
        obj = self.load(path=md_copy, catch_exception=catch_exception, **kargs)
        if date != None:
            obj = obj.filter(F.col('_updated') <= date)
        obj = dataframe.view(obj)

        return obj
    
    def load_raw_cdc(self, path=None, provider=None, catch_exception=True, **kargs):
        """
        Load all version of the cdc database
        
        :return: list of dataframes at each versions 
        """
        
        if isinstance(path, YamlDict):
            md = path.to_dict()
        elif isinstance(path, str):
            md = get_metadata(self._rootdir, self._metadata, path, provider)
        elif isinstance(path, dict):
            md = path
            
        try:
            versions = [x.name for x in self.list(md,path=md['resource_path']).select('name').collect()]
            versions = [version for version in versions if '_version=' in version]
        except:
            versions = []
            
        dataframes = []
        for version in versions:
            md_copy = copy.deepcopy(md)
            md_copy['url'] += '/' + version
            obj = self.load(path=md_copy, catch_exception=catch_exception, **kargs)
            dataframes.append(obj)
            
        return dataframes

    def load(self, path=None, provider=None, catch_exception=True, **kargs):
        if isinstance(path, YamlDict):
            md = path.to_dict()
        elif isinstance(path, str):
            md = get_metadata(self._rootdir, self._metadata, path, provider)
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
            'md': dict(md),
            'mode': kargs.get('mode', md.get('options', {}).get('mode')),
            'records': num_rows,
            'columns': num_cols,
            'time': prep_end - core_start,
            'time_core': core_end - core_start,
            'time_prep': prep_end - prep_start
        }
        logging.info(log_data) if obj is not None else logging.error(log_data)

        return obj

    def load_dataframe(self, md, catch_exception=True, **kargs):
        obj = None
        options = md['options']

        try:
            if md['service'] in ['local', 'file']:
                if md['format'] == 'csv':
                    try:
                        obj = self._ctx.read.options(**options).csv(md['url'], **kargs)
                    except:
                        obj = self._ctx.createDataFrame(pd.read_csv(md['url'], **kargs))

                elif md['format'] == 'json':
                    try:
                        obj = self._ctx.read.options(**options).json(md['url'], **kargs)
                    except:
                        obj = self._ctx.createDataFrame(pd.read_json(md['url'], **kargs))
                elif md['format'] == 'jsonl':
                    try:
                        obj = self._ctx.read.option('multiLine', True).options(**options).json(md['url'], **kargs)
                    except:
                        obj = self._ctx.createDataFrame(pd.read_json(md['url'], lines=True, **kargs))
                elif md['format'] == 'parquet':
                    try:
                        obj = self._ctx.read.options(**options).parquet(md['url'], **kargs)
                    except:
                        obj = self._ctx.createDataFrame(pd.read_parquet(md['url'], **kargs))
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return None

            elif md['service'] in ['hdfs', 'minio']:
                if md['format'] == 'csv':
                    obj = self._ctx.read.options(**options).csv(md['url'], **kargs)
                elif md['format'] == 'json':
                    obj = self._ctx.read.options(**options).json(md['url'], **kargs)
                elif md['format'] == 'jsonl':
                    obj = self._ctx.read.option('multiLine', True).options(**options).json(md['url'], **kargs)
                elif md['format'] == 'parquet':
                    obj = self._ctx.read.options(**options).parquet(md['url'], **kargs)
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return None

            elif md['service'] in ['sqlite', 'mysql', 'postgres', 'mssql', 'oracle']:

                obj = self._ctx.read \
                    .format('jdbc') \
                    .option('url', md['url']) \
                    .option("dbtable", md['resource_path']) \
                    .option("driver", md['driver']) \
                    .option("user", md['username']) \
                    .option('password', md['password']) \
                    .options(**options)

                # load the data from jdbc
                obj = obj.load(**kargs)
                
                                   
            elif md['service'] == 'mongodb':

                if '?' in md['url']:
                    connection_str = md['url'].split('?')[0] + '.' + md['resource_path'] + '?' +md['url'].split('?')[1]
                else:
                    connection_str = md['url']+ '.' + md['resource_path']

                obj = self._ctx.read \
                    .format(md['format']) \
                    .option('spark.mongodb.input.uri',connection_str ) \
                    .options(**options)
                                   
                # load the data                
                obj = obj.load(**kargs)
                                   
            elif md['service'] == 'elastic':
                results = elastic.read(md['url'], options.get('query', {}))
                rows = [pyspark.sql.Row(**r) for r in results]
                obj = self.context().createDataFrame(rows)
            else:
                logging.error({'md': md, 'error_msg': f'Unknown service "{md["service"]}"'})
        except Exception as e:
            if catch_exception:
                logging.error({'md': md, 'error': str(e)})
                return None
            else:
                raise e

        return obj

    def save(self, obj, path=None, provider=None, **kargs):

        if isinstance(path, YamlDict):
            md = path.to_dict()
        elif isinstance(path, str):
            md = get_metadata(self._rootdir, self._metadata, path, provider)
        elif isinstance(path, dict):
            md = path

        prep_start = timer()

        if md['date_partition'] and md['date_column']:
            tzone = 'UTC' if self._timestamps == 'naive' else self._timezone
            obj = dataframe.add_datetime_columns(obj, column=md['date_column'], tzone=tzone)
            kargs['partitionBy'] = ['_date'] + kargs.get('partitionBy', md.get('options', {}).get('partitionBy', []))

        if md['version_column']:
            kargs['partitionBy'] = ['_version'] + kargs.get('partitionBy', [])

        if md['update_column']:
            obj = dataframe.add_update_column(obj, tzone=self._timezone)

        if md['hash_column']:
            obj = dataframe.add_hash_column(obj, cols=md['hash_column'],
                                            exclude_cols=['_date', '_datetime', '_updated', '_hash', '_state', '_version'])

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
            'mode': kargs.get('mode', md.get('options', {}).get('mode')),
            'records': num_rows,
            'columns': num_cols,
            'time': core_end - prep_start,
            'time_core': core_end - core_start,
            'time_prep': prep_end - prep_start
        }

        logging.info(log_data) if result else logging.error(log_data)

        return result

    def save_dataframe(self, obj, md, **kargs):

        options = md.get('options', {})

        try:
            if md['service'] in ['local', 'file']:
                if md['format'] == 'csv':
                    try:
                        obj.write.options(**options).csv(md['url'], **kargs)
                    except:
                        obj.toPandas().to_csv(md['url'], **kargs)
                elif md['format'] == 'json':
                    try:
                        obj.write.options(**options).json(md['url'], **kargs)
                    except:
                        obj.toPandas().to_json(md['url'], **kargs)
                elif md['format'] == 'jsonl':
                    try:
                        obj.write.options(**options).option('multiLine', True).json(md['url'], **kargs)
                    except:
                        obj.toPandas().to_json(md['url'], orient='records', lines=True, **kargs)
                elif md['format'] == 'parquet':
                    try:
                        obj.write.options(**options).parquet(md['url'], **kargs)
                    except:
                        obj.toPandas().to_parquet(md['url'], orient='records', lines=True, **kargs)
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return False

            elif md['service'] in ['hdfs', 'minio']:
                if md['format'] == 'csv':
                    obj.write.options(**options).csv(md['url'], **kargs)
                elif md['format'] == 'json':
                    obj.write.options(**options).json(md['url'], **kargs)
                elif md['format'] == 'jsonl':
                    obj.write.options(**options).option('multiLine', True).json(md['url'], **kargs)
                elif md['format'] == 'parquet':
                    obj.write.options(**options).parquet(md['url'], **kargs)
                else:
                    logging.error({'md': md, 'error_msg': f'Unknown format "{md["format"]}"'})
                    return False

            elif md['service'] in ['sqlite', 'mysql', 'postgres', 'oracle']:
                obj.write \
                    .format('jdbc') \
                    .option('url', md['url']) \
                    .option("dbtable", md['resource_path']) \
                    .option("driver", md['driver']) \
                    .option("user", md['username']) \
                    .option('password', md['password']) \
                    .options(**options) \
                    .save(**kargs)
                                   
            elif md['service'] == 'mongodb':

                if '?' in md['url']:
                    connection_str = md['url'].split('?')[0] + '.' + md['resource_path'] + '?' +md['url'].split('?')[1]
                else:
                    connection_str = md['url']+ '.' + md['resource_path']

                obj.write \
                    .format(md['format']) \
                    .option('spark.mongodb.input.uri', connection_str) \
                    .options(**options)\
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

        # handle null field with mongodb
        if md_src['service'] == 'mongodb':
            for column_type in df_src.dtypes:
                if 'null' in column_type[1]:
                    df_src = df_src.withColumn(column_type[0], df_src[column_type[0]].cast(column_type[1].replace('null','string')))

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
            if md_trg['version_column']:
                df_src = dataframe.add_version_column(df_src, tzone=self._timezone)
            result = self.save(df_src, md_trg, mode=mode)

            log_data['time'] = timer() - timer_start
            log_data['records_read'] = num_rows
            log_data['records_add'] = num_rows
            log_data['columns'] = num_cols

            logging.notice(log_data) if result else logging.error(log_data)
            return

        # trg dataframe (if exists)
        try:
            if md_trg['version_column']:
                df_trg = self.load_cdc(md_trg, catch_exception=False)
            else:
                df_trg = self.load(md_trg, catch_exception=False)
        except:
            df_trg = dataframe.empty(df_src)

        # if there is schema change, create new version, log notice/error and return
        if not dataframe.compare_schema(df_src, df_trg, ['_date', '_datetime', '_updated', '_hash', '_state', '_version']):
            if md_trg['state_column']:
                df_src = df_src.withColumn('_state', F.lit(0))
            if md_trg['version_column']:
                df_src = dataframe.add_version_column(df_src, tzone=self._timezone)
            result = self.save(df_src, md_trg, mode=mode)

            log_data['time'] = timer() - timer_start
            log_data['records_read'] = num_rows
            log_data['records_add'] = num_rows
            log_data['columns'] = num_cols

            logging.notice(log_data) if result else logging.error(log_data)
            return

        # de-dup (exclude the _updated column)

        # create a view from the extracted log
        df_trg = dataframe.view(df_trg)
                               
        # capture added records
        df_add = dataframe.diff(df_src, df_trg, ['_date', '_datetime', '_updated', '_hash', '_state', '_version'])
        rows_add = df_add.count()

        # capture deleted records
        rows_del = 0
        if md_trg['state_column']:
            df_del = dataframe.diff(df_trg, df_src, ['_date', '_datetime', '_updated', '_hash', '_state', '_version'])
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
            
            if md_trg['version_column']:
                version = self.find_version(md=md_trg)
                date = datetime.strptime(version, '%Y-%m-%d-%H-%M-%S') if version else None
                df = dataframe.add_version_column(df, version_time=date, tzone=self._timezone)

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

    def list(self, provider,path=''):
        df_schema = T.StructType([
            T.StructField('name', T.StringType(), True),
            T.StructField('type', T.StringType(), True)])

        df_empty = self._ctx.createDataFrame(data=(), schema=df_schema)

        if isinstance(provider, str):
            md = get_metadata(self._rootdir, self._metadata, None, provider)
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
                        obj_type = 'FILE'
                    elif os.path.isdir(fullpath):
                        obj_type = 'DIRECTORY'
                    elif os.path.islink(fullpath):
                        obj_type = 'LINK'
                    elif os.path.ismount(fullpath):
                        obj_type = 'MOUNT'
                    else:
                        obj_type = 'UNDEFINED'
    
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
                provider_path = md['provider_path'] if md['service'] == 'hdfs' else '/'
                obj = fs.listStatus(Path(os.path.join(provider_path, path)))
                lst = []
    
                for i in range(len(obj)):
                    if obj[i].isFile():
                        obj_type = 'FILE'
                    elif obj[i].isDirectory():
                        obj_type = 'DIRECTORY'
                    else:
                        obj_type = 'UNDEFINED'
    
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
                              WHERE table_schema='{database}'
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
                return df_empty
        except Exception as e:
            logging.error({'md': md, 'error_msg': str(e)})
            raise e

        return df_empty


def get(name, md, rootdir):
    engine = NoEngine()

    if md.get('engine', {}).get('type') == 'spark':
        engine = SparkEngine(name, md, rootdir)

    # if md.get('engine', {}).get('type') == 'pandas':
    #      engine = PandasEngine(name, md, rootdir)

    return engine
