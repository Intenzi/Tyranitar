#   Copyright 2020-present Michael Hall
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Callable, Coroutine, TypeVar
from typing_extensions import ParamSpec

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
# type: ignore

# I have taken and modified the code from: https://github.com/unified-moderation-network/umn-async-utils/

# code below is modified from: https://github.com/python/cpython/blob/3.11/Lib/functools.py#L448-L477

# Which was originally:
# # Written by Nick Coghlan <ncoghlan at gmail.com>,
# Raymond Hettinger <python at rcn.com>,
# and Łukasz Langa <lukasz at langa.pl>.
#   Copyright (C) 2006-2013 Python Software Foundation.

# The license in it's original form may be found: https://github.com/python/cpython/blob/3.11/LICENSE
# And is also included in this repository as ``LICENSE_cpython``

# It's included in minimal, simplified form based on specific use


class _HashedSeq(list):
    """ This class guarantees that hash() will be called no more than once
        per element.  This is important because the lru_cache() will hash
        the key multiple times on a cache miss.
    """

    __slots__ = 'hashvalue'

    def __init__(self, tup, hash=hash):
        self[:] = tup
        self.hashvalue = hash(tup)

    def __hash__(self):
        return self.hashvalue


def make_key(
    args,
    kwds,
    kwd_mark = (object(),),
    fasttypes = {int, str},
    type=type,
    len=len
):
    """Make a cache key from optionally typed positional and keyword arguments
    The key is constructed in a way that is flat as possible rather than
    as a nested structure that would take more memory.
    If there is only a single argument and its data type is known to cache
    its hash value, then that argument is returned without a wrapper.  This
    saves space and improves lookup speed.
    """
    # All of code below relies on kwds preserving the order input by the user.
    # Formerly, we sorted() the kwds before looping.  The new way is *much*
    # faster; however, it means that f(x=1, y=2) will now be treated as a
    # distinct call from f(y=2, x=1) which will be cached separately.
    key = args
    if kwds:
        key += kwd_mark
        for item in kwds.items():
            key += item
    elif len(key) == 1 and type(key[0]) in fasttypes:
        return key[0]
    return _HashedSeq(key)


__all__ = ("taskcache",)


P = ParamSpec("P")
T = TypeVar("T")


def taskcache(
    ttl: float | None = None,
):
    """
    Decorator to modify coroutine functions to instead act as functions returning cached tasks.

    For general use, this leaves the end user API largely the same,
    while leveraging tasks to allow preemptive caching.

    Note: This uses the args and kwargs of the original coroutine function as a cache key.
    This includes instances (self) when wrapping methods.
    Consider not wrapping instance methods, but what those methods call when feasible in cases where this may matter.
    """

    def wrapper(
        coro: Callable[[P], Coroutine[Any, Any, T]]
    ) -> Callable[[P], asyncio.Task[T]]:

        internal_cache: dict[Any, asyncio.Task[T]] = {}

        def wrapped(*args: P.args, **kwargs: P.kwargs) -> asyncio.Task[T]:
            # prevent self object from being in args of the built hash
            key = make_key(args[1:], kwargs)
            try:
                # don't cache errors
                if internal_cache[key] not in (None, Exception, PlaywrightTimeoutError):
                    return internal_cache[key]
                else:
                    raise KeyError
            except KeyError:
                internal_cache[key] = task = asyncio.create_task(coro(*args, **kwargs))
                if ttl is not None:
                    # This results in internal_cache.pop(key, task) later
                    # while avoiding a late binding issue with a lambda instead
                    call_after_ttl = partial(
                        asyncio.get_running_loop().call_later,
                        ttl,
                        internal_cache.pop,
                        key,
                    )
                    task.add_done_callback(call_after_ttl)
                return task

        return wrapped

    return wrapper
