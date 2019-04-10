#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2018 herrlich10@gmail.com
#
# Permission is hereby granted, free of charge, to any person obtaining a copy 
# of this software and associated documentation files (the "Software"), to deal 
# in the Software without restriction, including without limitation the rights 
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell 
# copies of the Software, and to permit persons to whom the Software is 
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included 
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR 
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, 
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL 
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER 
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, 
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE 
# SOFTWARE.

from __future__ import print_function, division, absolute_import, unicode_literals
import sys, os, shlex, time, textwrap, re
import subprocess, multiprocessing, queue, threading, ctypes, uuid
import numpy as np

__author__ = 'herrlich10 <herrlich10@gmail.com>'
__version__ = '0.1.7'

# The following are copied from six
# =================================
if sys.version_info[0] == 3:
    string_types = (str,)
    from io import StringIO
else:
    string_types = (basestring,)
    from StringIO import StringIO

def add_metaclass(metaclass):
    """Class decorator for creating a class with a metaclass."""
    def wrapper(cls):
        orig_vars = cls.__dict__.copy()
        slots = orig_vars.get('__slots__')
        if slots is not None:
            if isinstance(slots, str):
                slots = [slots]
            for slots_var in slots:
                orig_vars.pop(slots_var)
        orig_vars.pop('__dict__', None)
        orig_vars.pop('__weakref__', None)
        return metaclass(cls.__name__, cls.__bases__, orig_vars)
    return wrapper
# =================================


def format_duration(duration, format='standard'):
    '''Format duration (in seconds) in a more human friendly way.
    '''
    if format == 'short':
        units = ['d', 'h', 'm', 's']
    elif format == 'long':
        units = [' days', ' hours', ' minutes', ' seconds']
    else: # Assume 'standard'
        units = [' day', ' hr', ' min', ' sec']
    values = [int(duration//86400), int(duration%86400//3600), int(duration%3600//60), duration%60]
    for K in range(len(values)): # values[K] would be the first non-zero value
        if values[K] > 0:
            break
    formatted = ((('%d' if k<len(values)-1 else '%.3f') % values[k]) + units[k] for k in range(len(values)) if k >= K)
    return ' '.join(formatted)


def cmd_for_exec(cmd, shell=False):
    ''' Format cmd appropriately for execution according to whether shell=True.

    Split a cmd string into a list, if shell=False.
    Join a cmd list into a string, if shell=True.
    Do nothing to callable.

    Parameters
    ----------
    cmd : str, list, or callable
    shell : bool
    '''
    # If shell=kwargs, its true value is inferred.
    if isinstance(shell, dict):
        shell = ('shell' in shell and shell['shell'])
    if not callable(cmd):
        if shell: # cmd string is required
            if not isinstance(cmd, string_types):
                cmd = ' '.join(cmd)
        else: # cmd list is required
            if isinstance(cmd, string_types):
                cmd = shlex.split(cmd) # Split by space, preserving quoted substrings
    return cmd


def cmd_for_disp(cmd):
    '''Format cmd for printing.
    
    Parameters
    ----------
    cmd : str, list, or callable
    '''
    if callable(cmd):
        return cmd
    elif isinstance(cmd, string_types):
        return ' '.join(shlex.split(cmd)) # Remove insignificant whitespaces
    else: # Assume a list of str
        return ' '.join(cmd)


def run(cmd, check=True, verbose=2):
    '''Run an external command line.
    
    This function is similar to subprocess.run introduced in Python 3.5, but
    provides a slightly simpler and perhaps more convenient API.

    Parameters
    ----------
    cmd : str or list
    '''
    cmd = cmd_for_exec(cmd)
    cmd_str = cmd_for_disp(cmd)
    if verbose > 0:
        print('>>', cmd_str)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
    res = {'cmd': cmd_str, 'pid': p.pid, 'output': [], 'start_time': time.time()}
    for line in iter(p.stdout.readline, b''): # The 2nd argument is sentinel character (there will be no ending empty line)
        res['output'].append(line.decode('utf-8'))
        if verbose > 1:
            print(res['output'][-1], end='')
    p.stdout.close() # Notify the child process that the PIPE has been broken
    res['returncode'] = p.wait()
    res['stop_time'] = time.time()
    if verbose > 0:
        print('>> Command finished in {0}.'.format(format_duration(res['stop_time'] - res['start_time'])))
    if res['returncode'] and check:
        raise RuntimeError(f'Error occurs when executing the following command (returncode={p.returncode}):\n{cmd_str}')
    return res


STDOUT = sys.stdout
STDERR = sys.stderr

class TeeOut(StringIO):
    def __init__(self, err=False, tee=True):
        super().__init__()
        self.err = err
        self.tee = tee

    def write(self, s):
        super().write(s)
        if self.err: # Always output error message
            STDERR.write(s)
        elif self.tee:
            STDOUT.write(s)


class PooledCaller(object):
    '''
    Execute multiple command line programs, as well as python callables, 
    asynchronously and parallelly across a pool of processes.
    '''
    def __init__(self, pool_size=None, verbose=1):
        if pool_size is None:
            self.pool_size = multiprocessing.cpu_count() * 3 // 4
        else:
            self.pool_size = pool_size
        self.verbose = verbose
        self.ps = []
        self.cmd_queue = [] # Queue for commands and callables, as well as any additional args
        self._n_cmds = 0 # Auto increased counter for generating cmd idx
        self._idx2pid = {}
        self._pid2job = {} # Hold all jobs for each wait()
        self._log = [] # Hold all jobs for all waits (entire execution history for this PooledCaller instance)
        self.res_queue = multiprocessing.Queue() # Queue for return values of executed python callables
 
    def run(self, cmd, *args, **kwargs):
        '''Asynchronously run command or callable (queued execution, return immediately).
        
        See subprocess.Popen() for more information about the arguments.

        Multiple commands can be separated with ";" and executed sequentially 
        within a single subprocess in linux/mac, only if shell=True.
        
        Python callable can also be executed in parallel via multiprocessing.
        Note that although return values of the callable are retrieved via PIPE,
        sometimes it could be advantageous to directly save the computation 
        results into a shared file (e.g., an HDF5 file), esp. when they're large.
        In the later case, a proper lock mechanism via multiprocessing.Lock() 
        is required.

        Parameters
        ----------
        cmd : list, str, or callable
            Computation in command line programs is handled with subprocess.
            Computation in python callable is handled with multiprocessing.
        shell : bool
            If provided, must be a keyword argument.
            If shell is True, the command will be executed through the shell.
        *args, **kwargs : 
            If cmd is a callable, *args and **kwargs are passed to the callable as its arguments.
            If cmd is a list or str, **kwargs are passed to subprocess.Popen().
        '''
        cmd = cmd_for_exec(cmd, shell=kwargs)
        self.cmd_queue.append((self._n_cmds, cmd, args, kwargs))
        self._n_cmds += 1 # Accumulate by each call to run(), and reset after wait()

    def _callable_wrapper(self, idx, cmd, *args, **kwargs):
        out = TeeOut(tee=(self.verbose > 1))
        err = TeeOut(err=True)
        sys.stdout = out # This substitution only affect spawned process
        sys.stderr = err
        res = None # Initialized in case of exception
        try:
            res = cmd(*args, **kwargs)
        except Exception as e:
            print('>> Error occurs in job#{0}'.format(idx), file=err)
            print('** ERROR:', e, file=err) # AFNI style error message
            raise e # Re-raise and let parent process to handle it
        finally:
            # Grab all output at the very end of the process (assume that there aren't too much of them)
            # TODO: This could be a potential bug...
            # https://ryanjoneil.github.io/posts/2014-02-14-capturing-stdout-in-a-python-child-process.html
            output = out.getvalue().splitlines(True) + err.getvalue().splitlines(True)
            self.res_queue.put([idx, res, output]) # Communicate return value and output (Caution: The underlying pipe has limited size. Have to get() soon in wait().)

    def _async_reader(self, idx, f, output_list, speed_up):
        while True: # We can use event to tell the thread to stop prematurely, as demonstrated in https://stackoverflow.com/questions/323972/is-there-any-way-to-kill-a-thread
            line = f.readline()
            line = line.decode('utf-8')
            if line: # This is not lock protected, because only one thread (i.e., this thread) is going to write
                output_list.append(line)
                if line.startswith('*') or line.startswith('\x1b[7m'): # Always print AFNI style WARNING and ERROR through stderr
                    # '\x1b[7m' and '\x1b[0m' are 'reverse' and 'reset' respectively (https://gist.github.com/abritinthebay/d80eb99b2726c83feb0d97eab95206c4)
                    print('>> Something happens in job#{0}'.format(idx), file=sys.stderr)
                    print(line, end='', file=sys.stderr)
                elif self.verbose > 1:
                    print(line, end='')
            else: # Empty line signifies the end of the spawned process
                break
            if not speed_up.is_set():
                time.sleep(0.1) # Don't need to poll for output too aggressively during run time

    def dispatch(self):
        # If there are free slot and more jobs
        while len(self.ps) < self.pool_size and len(self.cmd_queue) > 0:
            idx, cmd, args, kwargs = self.cmd_queue.pop(0)
            job = {'idx': idx, 'cmd': cmd_for_disp(cmd), 'output': []} # Create a job process only after it is popped from the queue
            if self.verbose > 0:
                print('>> job#{0}: {1}'.format(idx, job['cmd']))
            if callable(cmd):
                # TODO: Add an if-else branch here if shared memory doesn't work for wrapper 
                p = multiprocessing.Process(target=self._callable_wrapper, args=(idx, cmd) + args, kwargs=kwargs)
                p.start()
            else:
                # Use PIPE to capture output and error message
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
                # Capture output without blocking (the main thread) by using a separate thread to do the blocking readline()
                job['speed_up'] = threading.Event()
                job['watcher'] = threading.Thread(target=self._async_reader, args=(idx, p.stdout, job['output'], job['speed_up']), daemon=True)
                job['watcher'].start()
            self.ps.append(p)
            job['start_time'] = time.time()
            job['pid'] = p.pid
            job['uuid'] = uuid.uuid4().hex[:6]
            self._idx2pid[idx] = p.pid
            self._pid2job[p.pid] = job
            self._log.append(job)

    def _async_get_res(self, res_list):
        try:
            res = self.res_queue.get(block=False) # idx, return_value, output
        except queue.Empty:
            pass
        else:
            res_list.append(res[:2])
            if len(res) > 2: # For callable only
                job = self._pid2job[self._idx2pid[res[0]]]
                job['output'] = res[2]

    def wait(self, pool_size=None, return_codes=False, return_jobs=False):
        '''
        Wait for all jobs in the queue to finish.
        
        Returns
        -------
        return_values : list (if return_codes=False)
            Return value of python callable. Always `None` for command.
        codes : list (if return_codes=True)
            The return code of the child process for each job.
        jobs : list (if return_jobs=True, `jobs` will be the second return value)
            Detailed information about the child process, including captured stdout and stderr.
        '''
        if pool_size is not None:
            # Allow temporally adjust pool_size for current batch of jobs
            old_size = self.pool_size
            self.pool_size = pool_size
        start_time = time.time()
        ress = []
        while len(self.ps) > 0 or len(self.cmd_queue) > 0:
            # Dispatch jobs if possible
            self.dispatch()
            # Poll workers' state
            for p in self.ps:
                job = self._pid2job[p.pid]
                if isinstance(p, subprocess.Popen):
                    if p.poll() is not None: # If the process is terminated
                        job['stop_time'] = time.time()
                        job['returncode'] = p.returncode
                        job['speed_up'].set()
                        job['watcher'].join() # Retrieve all remaining output before closing PIPE
                        p.stdout.close() # Notify the child process that the PIPE has been broken
                        self.ps.remove(p)
                        if self.verbose > 0:
                            print('>> job#{0} finished in {1}.'.format(job['idx'], format_duration(job['stop_time']-job['start_time'])))
                        self.res_queue.put([job['idx'], None]) # Return None to mimic callable behavior
                        job.pop('watcher') # These helper objects may not be useful for the end users
                        job.pop('speed_up')
                    else:
                        pass
                elif isinstance(p, multiprocessing.Process):
                    if not p.is_alive(): # If the process is terminated
                        job['stop_time'] = time.time()
                        job['returncode'] = p.exitcode # subprocess.Popen and multiprocessing.Process use different names for this
                        self.ps.remove(p)
                        if self.verbose > 0:
                            print('>> job#{0} finished in {1}.'.format(job['idx'], format_duration(job['stop_time']-job['start_time'])))
                    else:
                        pass
            time.sleep(0.1)
            # Dequeuing, see https://stackoverflow.com/questions/10028809/maximum-size-for-multiprocessing-queue-item
            self._async_get_res(ress)
        # Handle return values by callable cmd
        self._async_get_res(ress)
        ress = [res[1] for res in sorted(ress, key=lambda res: res[0])]
        # Handle return codes by children processes
        jobs = sorted(self._pid2job.values(), key=lambda job: job['idx'])
        codes = [job['returncode'] for job in jobs]
        if self.verbose > 0:
            duration = time.time() - start_time
            print('>> All {0} jobs done in {1}.'.format(self._n_cmds, format_duration(duration)))
            if np.any(codes):
                print('returncodes: {0}'.format(codes))
            else:
                print('all returncodes are 0.')
            if self.all_successful(jobs=jobs):
                print('>> All {0} jobs finished successfully.'.format(len(jobs)))
            else:
                print('>> Please pay attention to the above errors.')
        # Reset object states
        self._n_cmds = 0
        self._idx2pid = {}
        self._pid2job = {}
        if pool_size is not None:
            self.pool_size = old_size
        if return_codes:
            return (codes, jobs) if return_jobs else codes
        else:
            return (ress, jobs) if return_jobs else ress

    def all_successful(self, error_pattern='error|\*{2}', jobs=None, verbose=None):
        if jobs is None:
            jobs = self._log
        if verbose is None:
            verbose = self.verbose
        # Check return codes
        all_zero = not np.any([job['returncode'] for job in jobs])
        # Check output
        n_errors = 0
        if error_pattern is not None:
            error_pattern = re.compile(error_pattern, re.IGNORECASE)
            for job in jobs:
                for line in job['output']:
                    if error_pattern.search(line):
                        if verbose > 0:
                            print('[job#{0}] {1}'.format(job['idx'], line), end='')
                        n_errors += 1
        return all_zero and n_errors == 0

    def idss(self, total, batch_size=None):
        if batch_size is None:
            batch_size = int(np.ceil(total / self.pool_size / 10))
        return (range(k, min(k+batch_size, total)) for k in range(0, total, batch_size))

    def __call__(self, job_generator):
        # This is similar to the joblib.Parallel signature. 
        # It allows each call to deal with a batch of jobs for better performance, 
        # especially useful when there are a huge amount of small jobs.
        # e.g. pc(pc.run(compute_depth, ids, *args) for ids in pc.idss(len(depths)))
        n_batches = 0
        for _ in job_generator:
            n_batches += 1
        if self.verbose > 0:
            print('>> Start with a total of {0} jobs...'.format(n_batches))
        self.wait()


class ArrayWrapper(type):
    '''
    This is the metaclass for classes that wrap an np.ndarray and delegate 
    non-reimplemented operators (among other magic functions) to the wrapped array.
    '''
    def __init__(cls, name, bases, dct):
        def make_descriptor(name):
            return property(lambda self: getattr(self.arr, name))

        type.__init__(cls, name, bases, dct)
        ignore = 'class mro new init setattr getattr getattribute'
        ignore = set('__{0}__'.format(name) for name in ignore.split())
        for name in dir(np.ndarray):
            if name.startswith('__'):
                if name not in ignore and name not in dct:
                    setattr(cls, name, make_descriptor(name))


@add_metaclass(ArrayWrapper) # Compatibility code from six
class SharedMemoryArray(object):
    '''
    This class can be used as a usual np.ndarray, but its data buffer
    is allocated in shared memory (under Cached Files in memory monitor), 
    and can be passed across processes without any data copy/duplication, 
    even when write access happens (which is lock-synchronized).

    The idea is to allocate memory using multiprocessing.Array, and  
    access it from current or another process via a numpy.ndarray view, 
    without actually copying the data.
    So it is both convenient and efficient when used with multiprocessing.

    This implementation also demonstrates the power of composition + metaclass,
    as opposed to the canonical multiple inheritance.
    '''
    def __init__(self, dtype, shape, initializer=None, lock=True):
        self.dtype = np.dtype(dtype)
        self.shape = shape
        if initializer is None:
            # Preallocate memory using multiprocessing is the preferred usage
            self.shared_arr = multiprocessing.Array(self.dtype2ctypes[self.dtype], int(np.prod(self.shape)), lock=lock)
        else:
            self.shared_arr = multiprocessing.Array(self.dtype2ctypes[self.dtype], initializer, lock=lock)
        if not lock:
            self.arr = np.frombuffer(self.shared_arr, dtype=self.dtype).reshape(self.shape)
        else:
            self.arr = np.frombuffer(self.shared_arr.get_obj(), dtype=self.dtype).reshape(self.shape)
 
    @classmethod
    def zeros(cls, shape, dtype=float, lock=True):
        '''
        Return a new array of given shape and dtype, filled with zeros.

        This is the preferred usage, which avoids holding two copies of the
        potentially very large data simultaneously in the memory.
        '''
        return cls(dtype, shape, lock=lock)

    @classmethod
    def from_array(cls, arr, lock=True):
        '''
        Initialize a new shared-memory array with an existing array.
        '''
        # return cls(arr.dtype, arr.shape, arr.ravel(), lock=lock) # Slow and memory inefficient, why?
        a = cls.zeros(arr.shape, dtype=arr.dtype, lock=lock)
        a[:] = arr # This is a more efficient way of initialization
        return a

    def __getattr__(self, attr):
        if attr in self._SHARED_ARR_ATTRIBUTES:
            return getattr(self.shared_arr, attr)
        else:
            return getattr(self.arr, attr)

    def __dir__(self):
        return list(self.__dict__.keys()) + self._SHARED_ARR_ATTRIBUTES + dir(self.arr)

    _SHARED_ARR_ATTRIBUTES = ['acquire', 'release', 'get_lock']

    # At present, only numerical dtypes are supported.
    dtype2ctypes = {
        bool: ctypes.c_bool,
        int: ctypes.c_long,
        float: ctypes.c_double,
        np.dtype('bool'): ctypes.c_bool,
        np.dtype('int64'): ctypes.c_long,
        np.dtype('int32'): ctypes.c_int,
        np.dtype('int16'): ctypes.c_short,
        np.dtype('int8'): ctypes.c_byte,
        np.dtype('uint64'): ctypes.c_ulong,
        np.dtype('uint32'): ctypes.c_uint,
        np.dtype('uint16'): ctypes.c_ushort,
        np.dtype('uint8'): ctypes.c_ubyte,
        np.dtype('float64'): ctypes.c_double,
        np.dtype('float32'): ctypes.c_float,
        }
        
