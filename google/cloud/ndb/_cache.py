# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import itertools
import warnings

from google.api_core import retry as core_retry

from google.cloud.ndb import _batch
from google.cloud.ndb import context as context_module
from google.cloud.ndb import tasklets

_LOCKED = b"0"
_LOCK_TIME = 32
_PREFIX = b"NDB30"

warnings.filterwarnings("always", module=__name__)


class ContextCache(dict):
    """A per-context in-memory entity cache.

    This cache verifies the fetched entity has the correct key before
    returning a result, in order to handle cases where the entity's key was
    modified but the cache's key was not updated.
    """

    def get_and_validate(self, key):
        """Verify that the entity's key has not changed since it was added
        to the cache. If it has changed, consider this a cache miss.
        See issue 13.  http://goo.gl/jxjOP"""
        entity = self[key]  # May be None, meaning "doesn't exist".
        if entity is None or entity._key == key:
            return entity
        else:
            del self[key]
            raise KeyError(key)

    def __repr__(self):
        return "ContextCache()"


def _future_result(result):
    """Returns a completed Future with the given result.

    For conforming to the asynchronous interface even if we've gotten the
    result synchronously.
    """
    future = tasklets.Future()
    future.set_result(result)
    return future


def _future_exception(error):
    """Returns a completed Future with the given exception.

    For conforming to the asynchronous interface even if we've gotten the
    result synchronously.
    """
    future = tasklets.Future()
    future.set_exception(error)
    return future


def _global_cache():
    """Returns the global cache for the current context."""
    return context_module.get_context().global_cache


class _GlobalCacheBatch(object):
    """Abstract base for classes used to batch operations for the global cache."""

    def full(self):
        """Indicates whether more work can be added to this batch.

        Returns:
            boolean: `False`, always.
        """
        return False

    def idle_callback(self):
        """Call the cache operation.

        Also, schedule a callback for the completed operation.
        """
        try:
            cache_call = self.make_call()
            if not isinstance(cache_call, tasklets.Future):
                cache_call = _future_result(cache_call)
        except Exception as error:
            cache_call = _future_exception(error)

        cache_call.add_done_callback(self.done_callback)

    def done_callback(self, cache_call):
        """Process results of call to global cache.

        If there is an exception for the cache call, distribute that to waiting
        futures, otherwise set the result for all waiting futures to ``None``.
        """
        exception = cache_call.exception()
        if exception:
            for future in self.futures:
                future.set_exception(exception)

        else:
            for future in self.futures:
                future.set_result(None)

    def make_call(self):
        """Make the actual call to the global cache. To be overridden."""
        raise NotImplementedError

    def future_info(self, key):
        """Generate info string for Future. To be overridden."""
        raise NotImplementedError


def _handle_transient_errors(read=False):
    """Decorator for global_XXX functions for handling transient errors.

    Will log as warning or reraise transient errors according to `strict_read` and
    `strict_write` attributes of the global cache and whether the operation is a read or
    a write.

    If in strict mode, will retry the wrapped function up to 5 times before reraising
    the transient error.
    """

    def wrap(wrapped):
        def retry(wrapped, transient_errors):
            @functools.wraps(wrapped)
            @tasklets.tasklet
            def retry_wrapper(*args, **kwargs):
                sleep_generator = core_retry.exponential_sleep_generator(0.1, 1)
                attempts = 5
                for sleep_time in sleep_generator:  # pragma: NO BRANCH
                    # pragma is required because loop never exits normally, it only gets
                    # raised out of.
                    attempts -= 1
                    try:
                        result = yield wrapped(*args, **kwargs)
                        raise tasklets.Return(result)
                    except transient_errors:
                        if not attempts:
                            raise

                    yield tasklets.sleep(sleep_time)

            return retry_wrapper

        @functools.wraps(wrapped)
        @tasklets.tasklet
        def wrapper(*args, **kwargs):
            cache = _global_cache()

            is_read = read
            if not is_read:
                is_read = kwargs.get("read", False)

            strict = cache.strict_read if is_read else cache.strict_write
            if strict:
                function = retry(wrapped, cache.transient_errors)
            else:
                function = wrapped

            try:
                if cache.clear_cache_soon:
                    warnings.warn("Clearing global cache...", RuntimeWarning)
                    cache.clear()
                    cache.clear_cache_soon = False

                result = yield function(*args, **kwargs)
                raise tasklets.Return(result)

            except cache.transient_errors as error:
                cache.clear_cache_soon = True

                if strict:
                    raise

                if not getattr(error, "_ndb_warning_logged", False):
                    # Same exception will be sent to every future in the batch. Only
                    # need to log one warning, though.
                    warnings.warn(
                        "Error connecting to global cache: {}".format(error),
                        RuntimeWarning,
                    )
                    error._ndb_warning_logged = True

                raise tasklets.Return(None)

        return wrapper

    return wrap


@_handle_transient_errors(read=True)
def global_get(key):
    """Get entity from global cache.

    Args:
        key (bytes): The key to get.

    Returns:
        tasklets.Future: Eventual result will be the entity (``bytes``) or
            ``None``.
    """
    batch = _batch.get_batch(_GlobalCacheGetBatch)
    return batch.add(key)


class _GlobalCacheGetBatch(_GlobalCacheBatch):
    """Batch for global cache get requests.

    Attributes:
        todo (Dict[bytes, List[Future]]): Mapping of keys to futures that are
            waiting on them.

    Arguments:
        ignore_options (Any): Ignored.
    """

    def __init__(self, ignore_options):
        self.todo = {}
        self.keys = []

    def add(self, key):
        """Add a key to get from the cache.

        Arguments:
            key (bytes): The key to get from the cache.

        Returns:
            tasklets.Future: Eventual result will be the entity retrieved from
                the cache (``bytes``) or ``None``.
        """
        future = tasklets.Future(info=self.future_info(key))
        futures = self.todo.get(key)
        if futures is None:
            self.todo[key] = futures = []
            self.keys.append(key)
        futures.append(future)
        return future

    def done_callback(self, cache_call):
        """Process results of call to global cache.

        If there is an exception for the cache call, distribute that to waiting
        futures, otherwise distribute cache hits or misses to their respective
        waiting futures.
        """
        exception = cache_call.exception()
        if exception:
            for future in itertools.chain(*self.todo.values()):
                future.set_exception(exception)

            return

        results = cache_call.result()
        for key, result in zip(self.keys, results):
            futures = self.todo[key]
            for future in futures:
                future.set_result(result)

    def make_call(self):
        """Call :method:`GlobalCache.get`."""
        return _global_cache().get(self.keys)

    def future_info(self, key):
        """Generate info string for Future."""
        return "GlobalCache.get({})".format(key)


@_handle_transient_errors()
def global_set(key, value, expires=None, read=False):
    """Store entity in the global cache.

    Args:
        key (bytes): The key to save.
        value (bytes): The entity to save.
        expires (Optional[float]): Number of seconds until value expires.
        read (bool): Indicates if being set in a read (lookup) context.

    Returns:
        tasklets.Future: Eventual result will be ``None``.
    """
    options = {}
    if expires is not None:
        # Actually testing if expires isnt set to any other value except None
        options = {"expires": expires}

    batch = _batch.get_batch(_GlobalCacheSetBatch, options)
    return batch.add(key, value)


class _GlobalCacheSetBatch(_GlobalCacheBatch):
    """Batch for global cache set requests. """

    def __init__(self, options):
        self.expires = options.get("expires")
        self.todo = {}
        self.futures = []

    def add(self, key, value):
        """Add a key, value pair to store in the cache.

        Arguments:
            key (bytes): The key to store in the cache.
            value (bytes): The value to store in the cache.

        Returns:
            tasklets.Future: Eventual result will be ``None``.
        """
        future = tasklets.Future(info=self.future_info(key, value))
        self.todo[key] = value
        self.futures.append(future)
        return future

    def make_call(self):
        """Call :method:`GlobalCache.set`."""
        return _global_cache().set(self.todo, expires=self.expires)

    def future_info(self, key, value):
        """Generate info string for Future."""
        return "GlobalCache.set({}, {})".format(key, value)


@_handle_transient_errors()
def global_delete(key):
    """Delete an entity from the global cache.

    Args:
        key (bytes): The key to delete.

    Returns:
        tasklets.Future: Eventual result will be ``None``.
    """
    batch = _batch.get_batch(_GlobalCacheDeleteBatch)
    return batch.add(key)


class _GlobalCacheDeleteBatch(_GlobalCacheBatch):
    """Batch for global cache delete requests."""

    def __init__(self, ignore_options):
        self.keys = []
        self.futures = []

    def add(self, key):
        """Add a key to delete from the cache.

        Arguments:
            key (bytes): The key to delete.

        Returns:
            tasklets.Future: Eventual result will be ``None``.
        """
        future = tasklets.Future(info=self.future_info(key))
        self.keys.append(key)
        self.futures.append(future)
        return future

    def make_call(self):
        """Call :method:`GlobalCache.delete`."""
        return _global_cache().delete(self.keys)

    def future_info(self, key):
        """Generate info string for Future."""
        return "GlobalCache.delete({})".format(key)


@_handle_transient_errors(read=True)
def global_watch(key):
    """Start optimistic transaction with global cache.

    A future call to :func:`global_compare_and_swap` will only set the value
    if the value hasn't changed in the cache since the call to this function.

    Args:
        key (bytes): The key to watch.

    Returns:
        tasklets.Future: Eventual result will be ``None``.
    """
    batch = _batch.get_batch(_GlobalCacheWatchBatch)
    return batch.add(key)


class _GlobalCacheWatchBatch(_GlobalCacheDeleteBatch):
    """Batch for global cache watch requests. """

    def __init__(self, ignore_options):
        self.keys = []
        self.futures = []

    def make_call(self):
        """Call :method:`GlobalCache.watch`."""
        return _global_cache().watch(self.keys)

    def future_info(self, key):
        """Generate info string for Future."""
        return "GlobalCache.watch({})".format(key)


@_handle_transient_errors()
def global_unwatch(key):
    """End optimistic transaction with global cache.

    Indicates that value for the key wasn't found in the database, so there will not be
    a future call to :func:`global_compare_and_swap`, and we no longer need to watch
    this key.

    Args:
        key (bytes): The key to unwatch.

    Returns:
        tasklets.Future: Eventual result will be ``None``.
    """
    batch = _batch.get_batch(_GlobalCacheUnwatchBatch)
    return batch.add(key)


class _GlobalCacheUnwatchBatch(_GlobalCacheWatchBatch):
    """Batch for global cache unwatch requests. """

    def make_call(self):
        """Call :method:`GlobalCache.unwatch`."""
        return _global_cache().unwatch(self.keys)

    def future_info(self, key):
        """Generate info string for Future."""
        return "GlobalCache.unwatch({})".format(key)


@_handle_transient_errors(read=True)
def global_compare_and_swap(key, value, expires=None):
    """Like :func:`global_set` but using an optimistic transaction.

    Value will only be set for the given key if the value in the cache hasn't
    changed since a preceding call to :func:`global_watch`.

    Args:
        key (bytes): The key to save.
        value (bytes): The entity to save.
        expires (Optional[float]): Number of seconds until value expires.

    Returns:
        tasklets.Future: Eventual result will be ``None``.
    """
    options = {}
    if expires:
        options["expires"] = expires

    batch = _batch.get_batch(_GlobalCacheCompareAndSwapBatch, options)
    return batch.add(key, value)


class _GlobalCacheCompareAndSwapBatch(_GlobalCacheSetBatch):
    """Batch for global cache compare and swap requests. """

    def make_call(self):
        """Call :method:`GlobalCache.compare_and_swap`."""
        return _global_cache().compare_and_swap(self.todo, expires=self.expires)

    def future_info(self, key, value):
        """Generate info string for Future."""
        return "GlobalCache.compare_and_swap({}, {})".format(key, value)


def global_lock(key, read=False):
    """Lock a key by setting a special value.

    Args:
        key (bytes): The key to lock.
        read (bool): Indicates if being called as part of a read (lookup) operation.

    Returns:
        tasklets.Future: Eventual result will be ``None``.
    """
    return global_set(key, _LOCKED, expires=_LOCK_TIME, read=read)


def is_locked_value(value):
    """Check if the given value is the special reserved value for key lock.

    Returns:
        bool: Whether the value is the special reserved value for key lock.
    """
    return value == _LOCKED


def global_cache_key(key):
    """Convert Datastore key to ``bytes`` to use for global cache key.

    Args:
        key (datastore.Key): The Datastore key.

    Returns:
        bytes: The cache key.
    """
    return _PREFIX + key.to_protobuf().SerializeToString()
