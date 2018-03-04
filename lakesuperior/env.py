import threading

'''
Thread-local bucket for switching configuration. Different environments
(e.g. webapp, test suite) put the appropriate value in it.
The most important values to be stored are app_conf (either from
lakesuperior.config_parser.config or lakesuperior.config_parser.test_config)
and app_globals (obtained by an instance of lakesuperior.globals.AppGlobals).

e.g.:

>>> from lakesuperior.config_parser import config
>>> from lakesuperior.globals import AppGlobals
>>> from lakesuperior.env import env
>>> env.config = config
>>> env.app_globals = AppGlobals(config)

This is automated in non-test environments by importing
`lakesuperior.env_setup`.

Context variables would be ideal: https://www.python.org/dev/peps/pep-0567/
However they are only available in Python 3.7. Better keep an eye on that.
'''
env = threading.local()