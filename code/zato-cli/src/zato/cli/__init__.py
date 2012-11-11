# -*- coding: utf-8 -*-

"""
Copyright (C) 2010 Dariusz Suchojad <dsuch at gefira.pl>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


# stdlib
import argparse, glob, logging, os, subprocess, sys, tempfile, time
from cStringIO import StringIO
from getpass import getpass, getuser
from socket import gethostname

# bzrlib
from bzrlib.lazy_import import lazy_import

# Zato
from zato.common.odb import engine_def
from zato.common.util import fs_safe_now

lazy_import(globals(), """
    # SQLAlchemy
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
""")

################################################################################

ZATO_LB_DIR = b'.zato-lb-dir'
ZATO_ADMIN_DIR = b'.zato-admin-dir'
ZATO_BROKER_DIR = b'.zato-broker-dir'
ZATO_SERVER_DIR = b'.zato-server-dir'

_opts_odb_type = 'ODB database type'
_opts_odb_host = 'ODB database host'
_opts_odb_port = 'ODB database port'
_opts_odb_user = 'ODB database user'
_opts_odb_schema = 'ODB database schema'
_opts_odb_dbname = 'ODB database name'
_opts_broker_host = 'broker host'
_opts_broker_start_port = 'broker starting port'
_opts_kvdb_host = 'kvdb database host'
_opts_kvdb_port = 'kvdb database port'

supported_db_types = ('oracle', 'postgresql')

ca_defaults = {
    'organization': 'My Company',
    'organizational_unit': 'My Unit',
    'locality': 'My Town',
    'state_or_province': 'My State',
    'country': 'US'
}

default_ca_name = 'Sample CA'
default_common_name = 'localhost'

common_odb_opts = [
        {'name':'odb_type', 'help':_opts_odb_type, 'choices':supported_db_types},
        {'name':'odb_host', 'help':_opts_odb_host},
        {'name':'odb_port', 'help':_opts_odb_port},
        {'name':'odb_user', 'help':_opts_odb_user},
        {'name':'odb_dbname', 'help':_opts_odb_dbname},
        {'name':'--odb-schema', 'help':_opts_odb_schema + ' (PostgreSQL only)'},
        {'name':'--odb-password', 'help':'ODB database password'},
]

broker_opts = [
    {'name':'broker_host', 'help':_opts_broker_host},
    {'name':'broker_start_port', 'help':_opts_broker_start_port},
]

common_ca_create_opts = [
    {'name=':'organization', 'help':'Organization name (defaults to {organization})'.format(**ca_defaults)},
    {'name=':'locality', 'help':'Locality name (defaults to {locality})'.format(**ca_defaults)},
    {'name=':'state-or-province', 'help':'State or province name (defaults to {state_or_province})'.format(**ca_defaults)},
    {'name=':'country', 'help':'Country (defaults to {country})'.format(**ca_defaults)},
    {'name=':'common-name', 'help':'Common name (defaults to {default})'.format(default=default_common_name)},
]

kvdb_opts = [
    {'name':'kvdb_host', 'help':_opts_kvdb_host},
    {'name':'kvdb_port', 'help':_opts_kvdb_port},
    {'name':'--kvdb-password', 'help':'kvdb database password'},
]

common_logging_conf_contents = """
[loggers]
keys=root,zato

[handlers]
keys=rotating_file_handler, stdout_handler

[formatters]
keys=default_formatter, colour_formatter

[logger_root]
level=INFO
handlers=rotating_file_handler, stdout_handler

[logger_zato]
level=INFO
handlers=rotating_file_handler, stdout_handler
qualname=zato
propagate=0

[handler_rotating_file_handler]
class=logging.handlers.RotatingFileHandler
formatter=default_formatter
args=('{log_path}', 'a', 20000000, 10)

[handler_stdout_handler]
class=StreamHandler
formatter=colour_formatter
args=(sys.stdout,)

[formatter_default_formatter]
format=%(asctime)s - %(levelname)s - %(process)d:%(threadName)s - %(name)s:%(lineno)d - %(message)s

[formatter_colour_formatter]
format=%(asctime)s - %(levelname)s - %(process)d:%(threadName)s - %(name)s:%(lineno)d - %(message)s
class=zato.common.util.ColorFormatter
"""

################################################################################

class ZatoCommand(object):
    """ A base class for all Zato CLI commands. Handles common things like parsing
    the arguments, checking whether a config file or command line switches should
    be used, asks for passwords etc.
    """
    needs_empty_dir = False
    file_needed = None
    needs_password_confirm = True
    add_batch = True
    add_config_file = True
    target_dir = None

    def __init__(self, args):
        self.verbose = args.verbose
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter('%(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        if args.store_log:
            verbose_handler = logging.FileHandler('zato.{}.log'.format(fs_safe_now()))
            verbose_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            verbose_handler.setFormatter(verbose_formatter)
            self.logger.addHandler(verbose_handler)
            
        if args.store_config:
            self.store_config(args)
        
        self.engine = None
        
    def _get_password(self, template, needs_confirm):
        """ Runs an infinite loop until a user enters the password. User needs
        to confirm the password if 'needs_confirm' is True. New line characters
        are always stripped before returning the password, so that "\n" becomes
        "", "\nsecret\n" becomes "secret" and "\nsec\nret\n" becomes "sec\nret".
        """
        keep_running = True
        self.logger.info('')

        while keep_running:
            password1 = getpass(template + ' (will not be echoed): ')
            if not needs_confirm:
                return password1.strip('\n')

            password2 = getpass('Enter the password again. Will not be echoed: ')

            if password1 != password2:
                self.logger.info('Passwords do not match')
            else:
                if not password1:
                    self.logger.info('No password entered')
                else:
                    return password1.strip('\n')

    def _get_now(self, time_=None):
        if not time_:
            time_ = time.gmtime()

        return time.strftime('%Y-%m-%d_%H-%M-%S', time_)

    def _get_user_host(self):
        return getuser() + '@' + gethostname()

    def store_config(self, args):
        """ Stores the config options in a config file for a later use.
        """
        now = fs_safe_now()
        file_name = 'zato.{}.config'.format(now)
        file_args = StringIO()

        for arg, value in args._get_kwargs():
            if value:
                file_args.write('{}={}\n'.format(arg, value))

        body = '# {} - {}\n{}'.format(now, self._get_user_host(), file_args.getvalue())

        open(file_name, 'w').write(body)
        file_args.close()

        self.logger.debug('Options saved in file {file_name}'.format(
            file_name=os.path.abspath(file_name)))

    def _get_engine(self, args):
        engine_url = engine_def.format(engine=args.odb_type, username=args.odb_user,
                        password=args.odb_password, host=args.odb_host, db_name=args.odb_dbname)
        return create_engine(engine_url)

    def _get_session(self, engine):
        Session = sessionmaker()
        Session.configure(bind=engine)
        return Session()

    def _check_passwords(self, args, check_password):
        """ Get the password from a user for each argument that needs a password.
        """
        for opt_name, opt_help in check_password:
            opt_name = opt_name.replace('--', '').replace('-', '_')
            if not getattr(args, opt_name, None):
                password = self._get_password(opt_help, self.needs_password_confirm)
                setattr(args, opt_name, password)

        return args

    def _get_arg(self, args, name, default):
        value = getattr(args, name, None)
        return value if value else default

    def run(self, args, offer_save_opts=True, work_args=None):
        """ Parses the command line or the args passed in and figures out
        whether the user wishes to use a config file or command line switches.
        """
        # Do we need to have a clean directory to work in?
        if self.needs_empty_dir:
            work_dir = os.path.abspath(os.path.join(os.getcwd(), self.target_dir))
            if os.listdir(work_dir):
                msg = ('Directory {} is not empty, please re-run the command ' +
                      'in an empty directory').format(work_dir)
                self.logger.info(msg)
                sys.exit(2)

        # Do we need the directory to contain any specific files?
        if self.file_needed:
            if not os.path.exists(os.path.join(self.target_dir, self.file_needed)):
                msg = self._on_file_missing(work_args)
                self.logger.info(msg)
                sys.exit(2)
        
        check_password = []
        for opt_dict in self.opts:
            name = opt_dict['name']
            if 'password' in name:
                check_password.append((name, opt_dict['help']))
        
        if check_password:
            args = self._check_passwords(args, check_password)
            
        self.execute(args)
        
class FromConfigFile(ZatoCommand):
    opts = []
    def execute(self, args):
        """ Runs the command with arguments read from a config file.
        """
        f = open(args.path)
        for line in f:
            if line.lstrip().startswith('#'):
                continue
            arg, value = line.split('=', 1)

            arg = arg.strip()
            value = value.strip()

            setattr(args, arg, value)

        if not self.batch:
            args = self._check_passwords(args, check_password)
        self.execute(args)

class CACreateCommand(ZatoCommand):
    """ A base class for all commands that create new crypto material.
    """
    file_needed = '.zato-ca-dir'

    def __init__(self, args):
        super(CACreateCommand, self).__init__(args)
        self.target_dir = os.path.abspath(args.path)

    def _on_file_missing(self):
        msg = "{} doesn't seem to be a CA directory, the '{}' file is missing."
        return msg.format(self.target_dir, self.file_needed)

    def _execute(self, args, extension):
        now = self._get_now()
        openssl_template = open(os.path.join(self.target_dir, 'ca-material/openssl-template.conf')).read()
        template_args = {}

        common_name = self._get_arg(args, 'common_name', default_common_name)
        organization = self._get_arg(args, 'organization', ca_defaults['organization'])
        organizational_unit = self._get_arg(args, 'organizational_unit', self.get_organizational_unit(args))
        locality = self._get_arg(args, 'locality', ca_defaults['locality'])
        state_or_province = self._get_arg(args, 'state_or_province', ca_defaults['state_or_province'])
        country = self._get_arg(args, 'country', ca_defaults['country'])

        template_args['common_name'] = common_name
        template_args['organization'] = organization
        template_args['organizational_unit'] = organizational_unit
        template_args['locality'] = locality
        template_args['state_or_province'] = state_or_province
        template_args['country'] = country

        template_args['target_dir'] = self.target_dir

        f = tempfile.NamedTemporaryFile()
        f.write(openssl_template.format(**template_args))
        f.flush()

        file_args = {
            'now':now,
            'target_dir':self.target_dir
        }

        for arg in('cluster_name', 'server_name'):
            if hasattr(args, arg):
                file_args[arg] = getattr(args, arg)

        file_args['file_prefix'] = self.get_file_prefix(file_args)

        csr_name = '{target_dir}/out-csr/{file_prefix}-csr-{now}.pem'.format(**file_args)
        priv_key_name = '{target_dir}/out-priv/{file_prefix}-priv-{now}.pem'.format(**file_args)
        pub_key_name = '{target_dir}/out-pub/{file_prefix}-pub-{now}.pem'.format(**file_args)
        cert_name = '{target_dir}/out-cert/{file_prefix}-cert-{now}.pem'.format(**file_args)

        format_args = {
            'config': f.name,
            'extension': extension,
            'csr_name': csr_name,
            'priv_key_name': priv_key_name,
            'pub_key_name': pub_key_name,
            'cert_name': cert_name,
            'target_dir': self.target_dir
        }

        # Create the CSR and keys ..
        cmd = """openssl req -batch -new -nodes -extensions {extension} \
                  -out {csr_name} \
                  -keyout {priv_key_name} \
                  -pubkey \
                  -newkey rsa:2048 -config {config} >/dev/null 2>&1""".format(**format_args)
        os.system(cmd)

        # .. note that we were using "-pubkey" flag above so we now have to extract
        # the public key from the CSR.

        split_line = '-----END PUBLIC KEY-----'
        csr_pub = open(csr_name).read()
        csr_pub = csr_pub.split(split_line)

        pub = csr_pub[0] + split_line
        csr = csr_pub[1].lstrip()

        open(csr_name, 'w').write(csr)
        open(pub_key_name, 'w').write(pub)

        # Generate the certificate
        cmd = """openssl ca -batch -passin file:{target_dir}/ca-material/ca-password -config {config} \
                 -out {cert_name} \
                 -extensions {extension} \
                 -in {csr_name} \
                 >/dev/null 2>&1""".format(**format_args)

        os.system(cmd)
        f.close()

        # Now delete the default certificate stored in './', we don't really
        # need it because we have its copy in './out-cert' anyway.
        last_serial = open(os.path.join(self.target_dir, 'ca-material/ca-serial.old')).read().strip()
        os.remove(os.path.join(self.target_dir, last_serial + '.pem'))

        msg = """Crypto material generated and saved in:
  - private key: {priv_key_name}
  - public key: {pub_key_name}
  - certificate {cert_name}
  - CSR: {csr_name}""".format(**format_args)

        if self.verbose:
            self.logger.debug(msg)
        else:
            self.logger.info('OK')

        # In case someone needs to invoke us directly and wants to find out
        # what the format_args were.
        return format_args

class ManageCommand(ZatoCommand):
    add_batch = False
    add_config_file = False

    def _get_dispatch(self):
        return {
            ZATO_ADMIN_DIR: self._on_zato_admin,
            ZATO_BROKER_DIR: self._on_broker,
            ZATO_LB_DIR: self._on_lb,
            ZATO_SERVER_DIR: self._on_server,
        }

    command_files = set([ZATO_ADMIN_DIR, ZATO_BROKER_DIR, ZATO_LB_DIR, ZATO_SERVER_DIR])

    opts = [
        dict(name="component_dir", help="A directory in which the component has been installed")
    ]

    def _zdaemon_start(self, contents_template,  zdaemon_conf_name,
                       socket_prefix, logfile_path_prefix, program):

        zdaemon_conf_name = os.path.join(self.config_dir, 'zdaemon', zdaemon_conf_name)

        socket_name = socket_prefix + '.sock'
        socket_name = os.path.join(self.config_dir, 'zdaemon', socket_name)

        logfile_path = logfile_path_prefix + '.log'
        logfile_path = os.path.join(self.component_dir, 'logs', logfile_path)

        conf = contents_template.format(program=program,
            socket_name=socket_name, logfile_path=logfile_path)

        open(zdaemon_conf_name, 'w').write(conf)
        self._execute_zdaemon_command(['zdaemon', '-C', zdaemon_conf_name, 'start'])

    def _zdaemon_command(self, zdaemon_command, zdaemon_config_pattern=['config', 'zdaemon', 'zdaemon*.conf']):

        conf_files = os.path.join(self.component_dir, *zdaemon_config_pattern)
        conf_files = sorted(glob.iglob(conf_files))

        prefix = os.path.join(self.component_dir, 'config', 'zdaemon', 'zdaemon')
        ports_pids = {}

        for conf_file in conf_files:
            port = conf_file.strip(prefix).strip('.').lstrip('0')
            pid = self._execute_zdaemon_command(['zdaemon', '-C', conf_file, zdaemon_command])
            ports_pids[port] = pid

            if zdaemon_command == 'stop':
                os.remove(conf_file)

        return ports_pids

    def _execute_zdaemon_command(self, command_list):
        p = subprocess.Popen(command_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()

        if p.returncode is None:
            msg = 'Could not execute command_list {0} (p.returncode is None)'
            msg = msg.format(str(command_list))
            raise Exception(msg)

        else:
            if p.returncode != 0:
                stdout, stderr = p.communicate()
                msg = 'Failed to execute command_list {0}.'
                msg += ' return code:[{1}], stdout:[{2}], stderr:[{3}]'
                msg = msg.format(str(command_list), p.returncode, stdout, stderr)
                raise Exception(msg)

            stdout, stderr = p.communicate()
            if stdout.startswith('program running'):
                data = stdout
                data = data.split(';')[1].strip()
                pid = data.split('=')[1].strip()

                return pid

    def execute(self, args):
        self.component_dir = os.path.abspath(args.component_dir)
        self.config_dir = os.path.join(self.component_dir, 'config')
        listing = set(os.listdir(self.component_dir))

        # Do we have any files we're looking for?
        found = self.command_files & listing

        if not found:
            msg = """\nDirectory {0} doesn't seem to belong to a Zato component. Expected one of the following files to exist {1}\n""".format(self.component_dir, sorted(self.command_files))
            self.logger.info(msg)
            sys.exit(2)

        elif len(found) > 1:
            msg = """\nExpected the directory {0} to contain exactly one of the following files {1}, found {2} instead.\n""".format(self.component_dir, sorted(self.command_files), sorted(found))
            self.logger.info(msg)
            sys.exit(2)

        found = list(found)[0]

        os.chdir(self.component_dir)
        self._get_dispatch()[found]()

