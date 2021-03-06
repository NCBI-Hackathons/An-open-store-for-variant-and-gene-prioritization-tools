import logging
import os
import time
import traceback
import argparse
from .inout import CravatReader
from .inout import CravatWriter
from .inout import AllMappingsParser
from cravat.config_loader import ConfigLoader
import sys
from .constants import crv_def
from .constants import crx_def
from .constants import crg_def
from .constants import all_mappings_col_name
from .constants import mapping_parser_name
from .exceptions import InvalidData
from .exceptions import ConfigurationError
import sqlite3
import json
import cravat.cravat_util as cu

class BaseAnnotator(object):

    valid_levels = ['variant','gene']
    valid_input_formats = ['crv','crx','crg']
    id_col_defs = {'variant':crv_def[0],
                   'gene':crg_def[0]}
    default_input_columns = {'crv':[x['name'] for x in crv_def],
                             'crx':[x['name'] for x in crx_def],
                             'crg':[x['name'] for x in crg_def]}
    required_conf_keys = ['level', 'output_columns']

    def __init__(self, cmd_args, status_writer):
        try:
            self.status_writer = status_writer
            self.logger = None
            main_fpath = cmd_args[0]
            main_basename = os.path.basename(main_fpath)
            if '.' in main_basename:
                self.annotator_name = '.'.join(main_basename.split('.')[:-1])
            else:
                self.annotator_name = main_basename
            self.annotator_dir = os.path.dirname(main_fpath)
            self.data_dir = os.path.join(self.annotator_dir, 'data')

            # Load command line opts
            self.primary_input_path = None
            self.secondary_paths = None
            self.output_dir = None
            self.output_basename = None
            self.plain_output = None
            self.job_conf_path = None
            self.parse_cmd_args(cmd_args)
            # Make output dir if it doesn't exist
            if not(os.path.exists(self.output_dir)):
                os.makedirs(self.output_dir)

            self._setup_logger()
            config_loader = ConfigLoader(self.job_conf_path)
            self.conf = config_loader.get_module_conf(self.annotator_name)
            self._verify_conf()
            self._id_col_name = self.conf['output_columns'][0]['name']
            if 'logging_level' in self.conf:
                self.logger.setLevel(self.conf['logging_level'].upper())
            if 'title' in self.conf:
                self.annotator_display_name = self.conf['title']
            else:
                self.annotator_display_name = os.path.basename(self.annotator_dir).upper()
            self.dbconn = None
            self.cursor = None
        except Exception as e:
            self._log_exception(e)

    def _log_exception(self, e, halt=True):
        if halt:
            raise e
        else:
            if self.logger:
                self.logger.exception(e)

    def _verify_conf(self):
        try:
            for k in self.required_conf_keys:
                if k not in self.conf:
                    err_msg = 'Required key "%s" not found in configuration' %k
                    raise ConfigurationError(err_msg)
            if self.conf['level'] in self.valid_levels:
                    self.conf['output_columns'] = [self.id_col_defs[self.conf['level']]] + self.conf['output_columns']
            else:
                err_msg = '%s is not a valid level. Valid levels are %s' \
                            %(self.conf['level'], ', '.join(self.valid_levels))
                raise ConfigurationError(err_msg)
            if 'input_format' in self.conf:
                if self.conf['input_format'] not in self.valid_input_formats:
                    err_msg = 'Invalid input_format %s, select from %s' \
                        %(self.conf['input_format'], ', '.join(self.valid_input_formats))
            else:
                if self.conf['level'] == 'variant':
                    self.conf['input_format'] = 'crv'
                elif self.conf['level'] == 'gene':
                    self.conf['input_format'] = 'crg'
            if 'input_columns' in self.conf:
                id_col_name = self.id_col_defs[self.conf['level']]['name']
                if id_col_name not in self.conf['input_columns']:
                    self.conf['input_columns'].append(id_col_name)
            else:
                self.conf['input_columns'] = self.default_input_columns[self.conf['input_format']]
        except Exception as e:
            self._log_exception(e)

    def _define_cmd_parser(self):
        try:
            parser = argparse.ArgumentParser()
            parser.add_argument('input_file',
                                help='Input file to be annotated.')
            parser.add_argument('-s',
                                action='append',
                                dest='secondary_inputs',
                                help='Secondary inputs. '\
                                     +'Format as <module_name>:<path>')
            parser.add_argument('-n',
                                dest='name',
                                help='Name of job. Default is input file name.')
            parser.add_argument('-d',
                                dest='output_dir',
                                help='Output directory. '\
                                     +'Default is input file directory.')
            parser.add_argument('-c',
                                dest='conf',
                                help='Path to optional run conf file.')
            parser.add_argument('-p', '--plainoutput',
                                action='store_true',
                                dest='plainoutput',
                                help='Skip column definition writing')
            self.cmd_arg_parser = parser
        except Exception as e:
            self._log_exception(e)

    # Parse the command line arguments
    def parse_cmd_args(self, cmd_args):
        try:
            self._define_cmd_parser()
            parsed_args = self.cmd_arg_parser.parse_args(cmd_args[1:])
            self.primary_input_path = os.path.abspath(parsed_args.input_file)
            self.secondary_paths = {}
            if parsed_args.secondary_inputs:
                for secondary_def in parsed_args.secondary_inputs:
                    sec_name, sec_path = secondary_def.split('@')
                    self.secondary_paths[sec_name] = os.path.abspath(sec_path)
            self.output_dir = os.path.dirname(self.primary_input_path)
            if parsed_args.output_dir:
                self.output_dir = parsed_args.output_dir

            self.plain_output = parsed_args.plainoutput
            self.output_basename = os.path.basename(self.primary_input_path)
            if parsed_args.name:
                self.output_basename = parsed_args.name
            if self.output_basename != '__dummy__':
                self.update_status_json_flag = True
            else:
                self.update_status_json_flag = False
            if parsed_args.conf:
                self.job_conf_path = parsed_args.conf
        except Exception as e:
            self._log_exception(e)

    # Runs the annotator.
    def run(self):
        if self.update_status_json_flag:
            self.status_writer.queue_status_update('status', 'Started {} ({})'.format(self.conf['title'], self.annotator_name))
        try:
            start_time = time.time()
            self.logger.info('started: %s'%time.asctime(time.localtime(start_time)))
            print('        {}: started at {}'.format(self.annotator_name, time.asctime(time.localtime(start_time))))
            self.base_setup()
            last_status_update_time = time.time()
            for lnum, line, input_data, secondary_data in self._get_input():
                try:
                    if self.update_status_json_flag:
                        cur_time = time.time()
                        if lnum % 10000 == 0 or cur_time - last_status_update_time > 3:
                            self.status_writer.queue_status_update('status', 'Running {} ({}): line {}'.format(self.conf['title'], self.annotator_name, lnum))
                            last_status_update_time = cur_time
                    if secondary_data == {}:
                        output_dict = self.annotate(input_data)
                    else:
                        output_dict = self.annotate(input_data, secondary_data)
                    # This enables summarizing without writing for now.
                    if output_dict == None:
                        continue
                    # Preserves the first column
                    output_dict[self._id_col_name] = input_data[self._id_col_name]
                    # Fill absent columns with empty strings
                    for output_col in self.conf['output_columns']:
                        col_name = output_col['name']
                        if col_name not in output_dict:
                            output_dict[col_name] = ''
                    self.output_writer.write_data(output_dict)
                except Exception as e:
                    self._log_runtime_exception(lnum, line, input_data, e)

            # This does summarizing.
            self.postprocess()

            self.base_cleanup()
            end_time = time.time()
            self.logger.info('finished: {0}'.format(time.asctime(time.localtime(end_time))))
            print('        {}: finished at {}'.format(self.annotator_name, time.asctime(time.localtime(end_time))))
            run_time = end_time - start_time
            self.logger.info('runtime: {0:0.3f}s'.format(run_time))
            print('        {}: runtime {:0.3f}s'.format(self.annotator_name, run_time))
            if self.update_status_json_flag:
                version = self.conf.get('version', 'unknown')
                self.status_writer.add_annotator_version_to_status_json(self.annotator_name, version)
                self.status_writer.queue_status_update('status', 'Finished {} ({})'.format(self.conf['title'], self.annotator_name))
        except Exception as e:
            self._log_exception(e)
        if hasattr(self, 'log_handler'):
            self.log_handler.close()
        if self.output_basename == '__dummy__':
            os.remove(self.log_path)

    def postprocess (self):
        pass

    async def get_gene_summary_data (self, cf):
        cols = [self.annotator_name + '__' + coldef['name'] \
                for coldef in self.conf['output_columns']]
        cols[0] = 'base__hugo'
        gene_collection = {}
        async for d in cf.get_variant_iterator_filtered_uids_cols(cols):
            hugo = d['hugo']
            if hugo == None:
                continue
            if hugo not in gene_collection:
                gene_collection[hugo] = {}
                for col in cols[1:]:
                    gene_collection[hugo][col.split('__')[1]] = []
            self.build_gene_collection(hugo, d, gene_collection)
        data = {}
        for hugo in gene_collection:
            out = self.summarize_by_gene(hugo, gene_collection)
            if out == None:
                continue
            row = []
            for col in cols[1:]:
                if col in out:
                    val = out[col]
                else:
                    val = None
                row.append(val)
            data[hugo] = out
        return data

    def _log_runtime_exception (self, lnum, line, input_data, e):
        try:
            err_str = traceback.format_exc().rstrip()
            if err_str not in self.unique_excs:
                self.unique_excs.append(err_str)
                self.logger.error(err_str)
            self.error_logger.error('\n[{:d}]{}\n({})\n#'.format(lnum, line[:-1], str(e)))
        except Exception as e:
            self._log_exception(e, halt=False)

    # Setup function for the base_annotator, different from self.setup() 
    # which is intended to be for the derived annotator.
    def base_setup(self):
        try:
            self._setup_primary_input()
            self._setup_secondary_inputs()
            self._setup_outputs()
            self._open_db_connection()
            self.setup()
        except Exception as e:
            self._log_exception(e)

    def _setup_primary_input(self):
        try:
            self.primary_input_reader = CravatReader(self.primary_input_path)
            requested_input_columns = self.conf['input_columns']
            defined_columns = self.primary_input_reader.get_column_names()
            missing_columns = set(requested_input_columns) - set(defined_columns)
            if missing_columns:
                if len(defined_columns) > 0:
                    err_msg = 'Columns not defined in input: %s' \
                        %', '.join(missing_columns)
                    raise ConfigurationError(err_msg)
                else:
                    default_columns = self.default_input_columns[self.conf['input_format']]
                    for col_name in requested_input_columns:
                        try:
                            col_index = default_columns.index(col_name)
                        except ValueError:
                            err_msg = 'Column %s not defined for %s format input' \
                                %(col_name, self.conf['input_format'])
                            raise ConfigurationError(err_msg)
                        if col_name == 'pos':
                            data_type = 'int'
                        else:
                            data_type = 'string'
                        self.primary_input_reader.override_column(col_index,
                                                                  col_name,
                                                                  data_type=data_type)
        except Exception as e:
            self._log_exception(e)

    def _setup_secondary_inputs(self):
        try:
            self.secondary_readers = {}
            try:
                num_expected = len(self.conf['secondary_inputs'])
            except KeyError:
                num_expected = 0
            num_provided = len(self.secondary_paths)
            if num_expected > num_provided:
                raise Exception('Too few secondary inputs. %d expected, ' +\
                    '%d provided'%(num_expected, num_provided))
            elif num_expected < num_provided:
                raise Exception('Too many secondary inputs. %d expected, %d provided'\
                        %(num_expected, num_provided))
            for sec_name, sec_input_path in self.secondary_paths.items():
                key_col = self.conf['secondary_inputs'][sec_name]\
                                                       ['match_columns']\
                                                       ['secondary']
                use_columns = self.conf['secondary_inputs'][sec_name]['use_columns']
                fetcher = SecondaryInputFetcher(sec_input_path,
                                                key_col,
                                                fetch_cols=use_columns)
                self.secondary_readers[sec_name] = fetcher
        except Exception as e:
            self._log_exception(e)

    # Open the output files (.var, .gen, .ncd) that are needed
    def _setup_outputs(self):
        try:
            level = self.conf['level']
            if level == 'variant':
                output_suffix = 'var'
            elif level == 'gene':
                output_suffix = 'gen'
            elif level == 'summary':
                output_suffix = 'sum'
            else:
                output_suffix = 'out'
            if not(os.path.exists(self.output_dir)):
                os.makedirs(self.output_dir)
            self.output_path = os.path.join(
                self.output_dir, 
                '.'.join([self.output_basename, 
                self.annotator_name,
                output_suffix]))
            self.invalid_path = os.path.join(
                self.output_dir, 
                '.'.join([self.output_basename, 
                self.annotator_name,
                'err']))
            if self.plain_output:
                self.output_writer = CravatWriter(self.output_path, 
                                                  include_definition = False,
                                                  include_titles = True,
                                                  titles_prefix = '')
            else:
                self.output_writer = CravatWriter(self.output_path)
                self.output_writer.write_meta_line('name',
                                                   self.annotator_name)
                self.output_writer.write_meta_line('displayname',
                                                   self.annotator_display_name)
            skip_aggregation = []
            for col_index, col_def in enumerate(self.conf['output_columns']):
                self.output_writer.add_column(col_index, col_def)
                if not(col_def.get('aggregate', True)):
                    skip_aggregation.append(col_def['name'])
            if not(self.plain_output):
                self.output_writer.write_definition(self.conf)
                self.output_writer.write_meta_line('no_aggregate',
                                                   ','.join(skip_aggregation))
        except Exception as e:
                self._log_exception(e)

    def _open_db_connection (self):
        db_dirs = [self.data_dir,
                   os.path.join('/ext', 'resource', 'newarch')]
        db_path = None
        for db_dir in db_dirs:
            db_path = os.path.join(db_dir, self.annotator_name + '.sqlite')
            if os.path.exists(db_path):
                self.dbconn = sqlite3.connect(db_path)
                self.cursor = self.dbconn.cursor()

    def close_db_connection (self):
        self.cursor.close()
        self.dbconn.close()

    def remove_log (self):
        pass

    def get_uid_col (self):
        return self.conf['output_columns'][0]['name']

    # Placeholder, intended to be overridded in derived class
    def setup(self):
        pass

    def base_cleanup(self):
        try:
            self.output_writer.close()
            #self.invalid_file.close()
            if self.dbconn != None:
                self.close_db_connection()
            self.cleanup()
        except Exception as e:
            self._log_exception(e)

    # Placeholder, intended to be overridden in derived class
    def cleanup(self):
        pass

    def remove_log_file (self):
        self.logger.removeHandler(self.log_handler)
        self.log_handler.flush()
        self.log_handler.close()
        os.remove(self.log_path)

    # Setup the logging utility
    def _setup_logger(self):
        try:
            self.logger = logging.getLogger('cravat.' + self.annotator_name)
            self.log_path = os.path.join(self.output_dir, self.output_basename + '.log')
            log_handler = logging.FileHandler(self.log_path, 'a')
            formatter = logging.Formatter('%(asctime)s %(name)-20s %(message)s', '%Y/%m/%d %H:%M:%S')
            log_handler.setFormatter(formatter)
            self.logger.addHandler(log_handler)
            self.error_logger = logging.getLogger('error.' + self.annotator_name)
            error_log_path = os.path.join(self.output_dir, self.output_basename + '.err')
            error_log_handler = logging.FileHandler(error_log_path, 'a')
            formatter = logging.Formatter('SOURCE:%(name)-20s %(message)s')
            error_log_handler.setFormatter(formatter)
            self.error_logger.addHandler(error_log_handler)
        except Exception as e:
            self._log_exception(e)
        self.unique_excs = []

    # Gets the input dict from both the input file, and 
    # any depended annotators depended annotator feature not complete.
    def _get_input(self):
        for lnum, line, reader_data in self.primary_input_reader.loop_data():
            try:
                input_data = {}
                for col_name in self.conf['input_columns']:
                    input_data[col_name] = reader_data[col_name]
                if all_mappings_col_name in input_data:
                    input_data[mapping_parser_name] = \
                        AllMappingsParser(input_data[all_mappings_col_name])
                secondary_data = {}
                for annotator_name, fetcher in self.secondary_readers.items():
                    input_key_col = self.conf['secondary_inputs']\
                                              [annotator_name]\
                                               ['match_columns']\
                                                ['primary']
                    input_key_data = input_data[input_key_col]
                    secondary_data[annotator_name] = fetcher.get(input_key_data)
                yield lnum, line, input_data, secondary_data
            except Exception as e:
                self._log_runtime_error(lnum, e)
                continue

    def annotate (self, input_data):
        sys.stdout.write('        annotate method should be implemented. ' +\
                'Exiting ' + self.annotator_display_name + '...\n')
        exit(-1)

class SecondaryInputFetcher():
    def __init__(self,
                 input_path,
                 key_col,
                 fetch_cols=[]):
        self.key_col = key_col
        self.input_path = input_path
        self.input_reader = CravatReader(self.input_path)
        valid_cols = self.input_reader.get_column_names()
        if key_col not in valid_cols:
            err_msg = 'Key column %s not present in secondary input %s' \
                %(key_col, self.input_path)
            raise ConfigurationError(err_msg)
        if fetch_cols:
            unmatched_cols = list(set(fetch_cols) - set(valid_cols))
            if unmatched_cols:
                err_msg = 'Column(s) %s not present in secondary input %s' \
                    %(', '.join(unmatched_cols), self.input_path)
                raise ConfigurationError(err_msg)
            self.fetch_cols = fetch_cols
        else:
            self.fetch_cols = valid_cols
        self.data = {}
        self.load_input()

    def load_input(self):
        for _, line, all_col_data in self.input_reader.loop_data():
            key_data = all_col_data[self.key_col]
            if key_data not in self.data: self.data[key_data] = []
            fetch_col_data = {}
            for col in self.fetch_cols:
                fetch_col_data[col] = all_col_data[col]
            self.data[key_data].append(fetch_col_data)

    def get(self, key_data):
        if key_data in self.data:
            return self.data[key_data]
        else:
            return []

    def get_values (self, key_data, key_column):
        ret = [v[key_column] for v in self.data[key_data]]
        return ret
