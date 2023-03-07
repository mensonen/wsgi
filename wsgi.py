import hashlib
import json
import logging
import re
import http.cookies
import pickle
import queue
import urllib.parse
import random
import sys
import threading
import time
import traceback
import os

from collections.abc import MutableMapping
from datetime import datetime
from functools import wraps
from typing import Any, List, NamedTuple, Optional, Union, Tuple, Type

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    _templating_available = True
except ImportError:
    Environment = FileSystemLoader = select_autoescape = None
    _templating_available = False

HTTP_STATUS_CODES = {
    100: "Continue",
    101: "Switching Protocols",
    102: "Processing",
    200: "OK",
    201: "Created",
    202: "Accepted",
    203: "Non Authoritative Information",
    204: "No Content",
    205: "Reset Content",
    206: "Partial Content",
    207: "Multi Status",
    226: "IM Used",
    300: "Multiple Choices",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    305: "Use Proxy",
    307: "Temporary Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Request Entity Too Large",
    414: "Request URI Too Long",
    415: "Unsupported Media Type",
    416: "Requested Range Not Satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    426: "Upgrade Required",
    428: "Precondition Required",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    449: "Retry With",
    451: "Unavailable For Legal Reasons",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
    507: "Insufficient Storage",
    510: "Not Extended"}


class TimeoutLockError(Exception):
    """Raised by `TimeoutLock` when no lock can be acquired."""
    pass


class TimeoutLock:
    """A wrapper around a single-entry FIFO queue with a context manager.

    A thread-safe way to ensure that only one caller at a time can perform a
    task, while other threads/callers are put in a queue and are given access
    in turn.

    While a caller holds a lock, others attempting to gain the lock either
    block until timeout (default 30 seconds) is reached and a `TimeoutLockError`
    is raised, or access to the lock is granted.

    >>> lock = TimeoutLock()
    >>> # from thread #1
    >>> with lock:
    >>>     time.sleep(32)
    >>> # from thread #2, this will raise a TimeoutLockerror, as thread#1 holds
    >>> # lock for over 30 seconds
    >>> with lock:
    >>>     print("my turn")

    When creating multiple locks on the fly, `TimeoutLockManager` can automate
    lock creation and removal.
    """
    def __init__(self, timeout=30):
        self._lock = queue.Queue(1)
        self.timeout = timeout

    def __enter__(self):
        self.lock()
        return self

    def __exit__(self, exittype, value, traceback):
        self.unlock()

    def wait(self):
        """Block until nobody holds a lock anymore."""
        self._lock.join()

    def lock(self):
        """Acquire a lock."""
        try:
            self._lock.put(1, block=True, timeout=self.timeout)
        except queue.Full:
            raise TimeoutLockError().with_traceback(sys.exc_info()[2])

    def unlock(self):
        """Release a lock."""
        self._lock.get()
        self._lock.task_done()


class TimeoutLockManager:
    """A callable dictionary to manage and automatically create timeout locks.

    Calling a lock manager with a named lock will automatically create a new
    lock object, if one does not yet exist and returns an instance of
    `TimeoutLock`.

    Locks can optionally expire by setting the `lock_ttl` to a non-None value
    (i.e lock time to live, in seconds). If set, will launch a background
    thread that periodically cleans up locks that have not been accessed since
    `lock_ttl` seconds.

    If `lock_ttl` is set, and it is expected that TimeoutLockManagers are created
    and removed on the fly, the stop() method should be called before deleting
    the lock manager in order to clean up the background thread.

    Examples:
    >>> locks = TimeoutLockManager()
    >>> locks("sim_12").wait()  # blocks, if someone is locking
    >>> with locks("sim_22", 10):
    >>>     # locks, causing anyone currently calling "sim_22" to block and
    >>>     # fail with a TimeOutLockError after a 10 second wait
    >>>     pass
    """
    def __init__(self, lock_ttl: int = None):
        self._locks = {}
        self._lock_ttl = lock_ttl

        self._stop_timer = False

        if self._lock_ttl is not None:
            self.expiration_timer = threading.Thread(target=self._expire_locks)
            self.expiration_timer.daemon = True
            self.expiration_timer.start()

    def __call__(self, lock_name: str, timeout: int = 30):
        if lock_name not in self._locks:
            self._locks[lock_name] = {"lock": TimeoutLock(timeout),
                                      "atime": time.time()}
        else:
            self._locks[lock_name]["atime"] = time.time()

        return self._locks[lock_name]["lock"]

    def __len__(self):
        return len(self._locks)

    def _expire_locks(self):
        while True:
            if self._stop_timer:
                return

            time.sleep(random.randint(30, 60))

            interval = int(time.time())
            expired_locks = []
            for lock_name, lock in self._locks.items():
                if interval - lock["atime"] > self._lock_ttl:
                    expired_locks.append(lock_name)

            for lock_name in expired_locks:
                self.remove(lock_name)

    def remove(self, lock_name: str):
        """Remove an unused lock."""
        if lock_name in self._locks:
            del self._locks[lock_name]

    def stop(self):
        """Stop cleanup thread."""
        self._stop_timer = True


session_locks = TimeoutLockManager(lock_ttl=30)


class FileSession(MutableMapping):
    """A file-based session.

    Automatically saves and loads session state to disk using pickle whenever
    dictionary entries change.

    The session class needs to be initiated at least once, and that instance
    has to be called for each session ID:

    >>> session_class = FileSession("/tmp")
    >>> s1 = session_class("1")  # get a session
    >>> s2 = session_class("2")  # get a different session

    """
    def __call__(self, sid: str, *args, **kwargs):
        return FileSession(self.dir_path, sid, *args, **kwargs)

    def __init__(self, dir_path: str, sid: str = None, *args: Any, **kwargs: Any):
        """Create a new file-based session storage.

        Args:
            dir_path: Location on the system to store session files in
            sid: Session ID
            *args: Optional initial session data as values
            **kwargs: Optional initial session data as key-value pairs

        """
        self.exists = False
        self.dir_path = self.file_path = dir_path
        if sid:
            self.file_path = os.path.join(dir_path, sid)
        self.sid = sid

        if args:
            self._data = dict(args)
        elif kwargs:
            self._data = kwargs
        else:
            self._data = {}

        self._load()

    def __delitem__(self, key):
        del self._data[key]
        self._save()

    def __getitem__(self, key):
        self._load()
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __setitem__(self, key, value):
        self._data[key] = value
        self._save()
        self.modified = True

    def _load(self):
        if self.sid:
            try:
                with open(self.file_path, "rb") as f:
                    self._data = pickle.load(f)
                    self.exists = True
            except (IOError, EOFError, pickle.UnpicklingError):
                pass
            except Exception as e:
                logging.getLogger("wsgi.FileSession").warning(
                    f"failed to load session: {e}")

    def _save(self):
        if self.sid:
            with open(self.file_path, "wb") as f:
                try:
                    pickle.dump(self._data, f, pickle.HIGHEST_PROTOCOL)
                    self.exists = True
                except Exception as e:
                    logging.getLogger("wsgi.FileSession").warning(
                        f"failed to save session: {e}")

    def destroy(self):
        """Erase session and its data on disk."""
        self._data = {}
        try:
            if self.sid:
                os.remove(self.file_path)
        except Exception:
            pass

    def save(self):
        """Force a session save.

        Session is automatically saved whenever one of its top-level attributes
        is altered. A session will not save if one contents of its mutable
        properties are altered.

        >>> s = FileSession("/tmp", 1)
        >>> s["customer"] = "Anonymous"  # auto-saved
        >>> s["cart"] = {"item_count": 0}  # auto-saved
        >>> s["cart"]["item_count"] += 1  # NOT auto-saved
        >>> s.save()

        """
        self._save()


class LockingFileSession(FileSession):
    """A thread-safe file session.

    Wraps `FileSession`, allowing only a single thread at a time to load a
    session or update session keys. Other threads attempting to alter the
    session at the same time will block until the session is again accessible,
    or a timeout occurs (30 seconds).

    """

    def _load(self):
        with session_locks(self.sid, timeout=2):
            super()._load()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        with session_locks(self.sid, timeout=2):
            super().__setitem__(key, value)

    def destroy(self):
        """Destroy and erase session data."""
        with session_locks(self.sid, timeout=2):
            super().destroy()


memory_sessions = {}


class MemorySession(MutableMapping):
    """A memory-based session.

    A very simple memory-based session that does not persist over restarts.
    Does by default nothing at all until the first session entry is saved.
    """

    def __init__(self, sid: str, *args: Any, **kwargs: Any):
        """Create a new memory-based session.

        Args:
            sid: Session ID
            *args: Initial session data as a value list
            **kwargs: Initial session data as a key-value list

        """
        self.sid = sid
        if self.sid in memory_sessions:
            self._data = memory_sessions[self.sid]
        elif args:
            self._data = dict(args)
        elif kwargs:
            self._data = kwargs
        else:
            self._data = {}

    def __delitem__(self, key):
        del self._data[key]

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __setitem__(self, key, value):
        self._check_and_create()
        self._data[key] = value

    def _check_and_create(self):
        if self.sid not in memory_sessions:
            memory_sessions[self.sid] = {}
            self._data = memory_sessions[self.sid]

    def destroy(self):
        self._data = {}

    def save(self):
        pass


class WsgiApp:
    """Basic WSGI HTTP request handler application.

    To be used together with a WSGI compliant web server, this class can be
    passed as a parameter to the WSGI server to catch all HTTP requests and
    process them. Implementing parties should subclass this and put
    user-functionality in the DELETE, GET, POST and PUT methods. The implemented
    methods should return two values: content to be returned to browser and a
    list of headers as tuples to be set.

    This handler also sets automatically a session cookie for each request and
    exposes the session id in `self.env["SESSION_ID"]`. To store data in session,
    the implementing class should use the self.session dictionary, which by
    default is a MemorySession object (does not persist over server restarts).

    Normally this class is never called directly, it is intended to be used
    together with the WsgiAppDispatcher, which maps request paths to
    applications.

    Sample usage:

    >>> class MyApp(WsgiApp):
    >>>      def GET(self, path=None):
    >>>      return str(path), [("Content-Type", "text/html")]
    >>>
    >>> # map all URLs to MyApp, catching the part after /
    >>> urls = [("/(.*)?", MyApp)]
    >>>
    >>> app = WsgiAppDispatcher(urls)
    >>>
    >>> server = cheroot.wsgi.Server(("localhost", 8080), app)
    >>> server.start()
    """
    args: dict
    """URL query parameters, or an empty dictionary."""
    headers: dict
    """Request headers, with header names written in Header-Notation."""
    debug: bool = False
    """Debug mode.

    When True, detailed errors are shown in browser. When False, only 500 
    Server errors are returned.
    """
    server_version: str = "Python/WSGI"
    """Server version, set as the "Server" header in all responses."""
    session_class: Type[MutableMapping] = MemorySession
    """Session class to use.

    Overwrite this to change from `MemorySession` to e.g `FileSession`. Can 
    be any dict-like mutable interface.
    """
    routes = {"GET": [], "POST": [], "PUT": [], "DELETE": []}
    template_path: str = "templates/"
    """Path for templates.
    Either an absolute path or a path relative to the current python module. 
    See `render_template` for details.
    """
    url: urllib.parse.ParseResult
    """Request URL as a result of `urllib.parse.urlparse`."""

    def __init__(self, dispatcher_matches: List[str] = None):
        """Creates a new app request instance.

        A new instance should never be created manually. Instead, pass the class
        itself as an argument to `WsgiAppDispatcher`, which will create a new
        app instance for each incoming request.

        Args:
            dispatcher_matches: List of matched capture groups in a URL. Filled
                in automatically by WsgiAppDispatcher when a new request is
                created.

        """
        self.dispatcher_matches = dispatcher_matches or []

        self.template_env = None
        if _templating_available:
            self.template_env = Environment(
                loader=FileSystemLoader(self.template_path),
                autoescape=select_autoescape(["html", "xml"]))

    def __call__(self, env, start_response):
        time_start = time.time()
        headercookie = env.get("HTTP_COOKIE")

        cookie = http.cookies.SimpleCookie()
        if headercookie:
            cookie.load(headercookie)

        if "session_id" not in cookie:
            session_id = hashlib.sha1("{}{}{}".format(
                os.urandom(16), time.time(), env.get("REMOTE_ADDR")).encode())
            cookie["session_id"] = session_id.hexdigest()

        session_cookie = cookie["session_id"]

        env["SESSION_ID"] = cookie["session_id"].value

        self._input = None

        self.base_path = ""
        self.env = env
        self.url = urllib.parse.urlparse(env.get("REQUEST_URI"))
        self.session = self.session_class(env["SESSION_ID"])

        if self.url.query:
            self.args = parse_qs_dict(self.url.query)
        else:
            self.args = {}

        self.headers = {}
        for key, value in env.items():
            if key.startswith("HTTP_"):
                header_name = "-".join(w.capitalize() for w in key.split("_")[1:])
                self.headers[header_name] = value

        headers = [("Content-Type", "text/html")]
        http_status = "200 OK"
        response = ""

        conversion_methods = {"int": int, "str": str, "bool": bool, "float": float}

        try:
            routed_method = None
            route_path_matches = None

            if self.routes and self.routes[env["REQUEST_METHOD"]]:
                for p, method in self.routes[env["REQUEST_METHOD"]]:
                    try:
                        # look for <type>:<param_name> pairs in the route path
                        # and replace them with just <param_name>
                        convert: dict = {}
                        param_conversions = re.findall(r"<(int|str|bool|float):(.+?)>", p)
                        if param_conversions:
                            for t, param_name in param_conversions:
                                convert[param_name] = conversion_methods[t]
                            p = re.sub(r"<(int|str|bool|float):(.+?)>", "<\\2>", p)

                        # convert <param_name> to a python regex named capture
                        # group that matches any word, i.e (?P<param_name>\w+)
                        path_match = re.sub("<(.+?)>", "(?P<\\1>[^/]+)", p)

                        if self.dispatcher_matches:
                            match_against = self.dispatcher_matches[0]
                            self.base_path = env["PATH_INFO"].replace(match_against, "")
                        else:
                            match_against = env["PATH_INFO"]
                            self.base_path = ""

                        result = re.match("^" + path_match + "$", match_against)
                        if result:
                            routed_method = method
                            route_path_matches = result.groupdict()
                            break

                    except re.error as e:
                        print(f"Path regular expression error: {str(e)} for "
                              f"path {env['PATH_INFO']}")

            self.before_request()

            if routed_method:
                for match_param, match_value in route_path_matches.items():
                    if match_param in convert:
                        try:
                            route_path_matches[match_param] = convert[match_param](match_value)
                        except Exception:
                            pass
                response = routed_method(self, **route_path_matches)
            elif env["REQUEST_METHOD"] == "POST":
                response = self.POST(*self.dispatcher_matches)

            elif env["REQUEST_METHOD"] == "GET":
                response = self.GET(*self.dispatcher_matches)

            elif env["REQUEST_METHOD"] == "PUT":
                response = self.PUT(*self.dispatcher_matches)

            elif env["REQUEST_METHOD"] == "DELETE":
                response = self.DELETE(*self.dispatcher_matches)

            else:
                raise WsgiMethodNotallowed()

            if isinstance(response, tuple):
                response, headers = response

            elif isinstance(response, WsgiHttpResponse):
                http_status = response.http_status
                headers = response.headers
                response = response.data or ""

        except WsgiHttpError as e:
            http_status = e.http_status
            response = e.html_body
            headers = e.headers

        except Exception as e:
            http_status = "500 Internal Server Error"
            if self.debug:
                response = repr(e)
            else:
                response = ""

            logging.getLogger("WsgiApp").warning(
                f"WSGI app raised an exception: {repr(e)}")
            exc_info = sys.exc_info()
            logging.getLogger("WsgiApp").error(
                "".join(traceback.format_exception(*exc_info)))

        set_cookie = "-"
        if session_cookie is not None:
            set_cookie = session_cookie.OutputString()
            headers.append(("Set-Cookie", set_cookie))

        if not response:
            response = ""

        if isinstance(response, str):
            response = response.encode("utf-8")

        length = str(len(response))
        headers.append(("Content-Length", length))
        headers.append(("Server", self.server_version))

        self.before_response(http_status, headers, response)
        start_response(http_status, headers)

        time_finish = time.time()
        delta = f"{time_finish - time_start:6f}"
        print('{} - - [{}] "{}" {} {} {} {}'.format(
            env.get("REMOTE_ADDR") or "-",
            datetime.now().replace(microsecond=0),
            env.get("PATH_INFO") or "",
            (http_status or "000").split()[0],
            length,
            delta,
            set_cookie))

        return [response]

    def _read_input(self):
        if self._input is None:
            if "CONTENT_LENGTH" in self.env:
                length = int(self.env.get("CONTENT_LENGTH", 0))
                self._input = self.env["wsgi.input"].read(length)
            else:
                # chunked transfer encoding, so read the whole input anyway
                self._input = self.env["wsgi.input"].read()

        return self._input

    @property
    def data(self) -> bytes:
        """HTTP request body in raw format"""
        return self._read_input()

    @property
    def form_data(self) -> dict:
        """HTTP request body parsed as a dict.

        This does not expect a form-data Content-Type header, but in order for
        the parsing to work, the request body needs to be in a format at least
        similar to form-data.
        """
        return parse_qs_dict(self._read_input().decode())

    @property
    def json(self) -> dict:
        """HTTP request body parsed as JSON.

        Assumes that the request body is a well-formed JSON string. No check for
        headers is made, attempting to read JSON input when the request contains
        no actual JSON data will raise an exception.
        """
        return json.loads(self._read_input())

    @classmethod
    def route(cls, path: str, methods: Union[List[str], str, None] = None):
        """Route specific paths inside an app to a method.

        For apps that handle multiple different paths with different logic, this
        method can be used to "sub" route them further, instead of attempting
        to parse self.url.path inside the app, or instead of creating an app for
        every possible path.

        Args:
            path: The request path to route to the method. If the path
                pattern in the `WsgiAppDispatcher` setup has a capture group, the
                path is matched against that group. If no capture group is
                defined, the match is done against whole URL. The path can
                contain placeholders for parts that are dynamic, enclosed in `<`
                and `>`, e.g `<user_name>` or `<int:user_id>`. If placeholder is
                prefixed with an "int:", "bool:", "float:" or "str:", the value
                is converted respectively. Any placeholders are passed onto the
                routed method as keyword arguments.
            methods: A list of HTTP methods to route to method, or a single
                method, defaults to GET

        Usage:
        >>> class CustomerApp(WsgiApp):
        >>>
        >>>     # this matches /customer_service/customers
        >>>     @WsgiApp.route("customers")
        >>>     def get_customers(self):
        >>>         return json_response()
        >>>
        >>>     # this matches /customer_service/customer
        >>>     @WsgiApp.route("customer", methods=["PUT"])
        >>>     def create_customer(self):
        >>>         return json_response()
        >>>
        >>>     # this matches /customer_service/customer/4545
        >>>     @WsgiApp.route("customer/<int:customer_id>")
        >>>     def get_customer_by_id(self, customer_id):
        >>>         return json_response(id=customer_id)
        >>>
        >>>     def GET(self, *args, **kwargs):
        >>>         return WsgiNotFound("Fallback when path not found")
        >>>
        >>> class SimApp(WsgiApp):
        >>>
        >>>     # this matches /sim_service/sims. Note that the path given in
        >>>     # WsgiAppDispatcher has no capture group, so the .route path
        >>>     # matches the whole path instead
        >>>     @WsgiApp.route("/sim_service/sims")
        >>>     def get_sims(self):
        >>>         return json_response()
        >>>
        >>> app = WsgiAppDispatcher([
        >>>     ("/customer_service/(.+)", CustomerApp),
        >>>     ("/sim_service/.+", SimApp)
        >>> ])

        """
        if not methods:
            methods = ["GET"]
        if not isinstance(methods, list):
            methods = [methods]

        def wrapper(f):
            for method in methods:
                cls.routes[method].append((path, f))

            @wraps(f)
            def decorator(self, *args, **kwargs):
                return f(self, *args, **kwargs)

            return decorator

        return wrapper

    def DELETE(self, *args):
        """Method called for an HTTP DELETE request.

        This method is called if the HTTP REQUEST_METHOD is "DELETE", unless a
        method routed using `route` is called instead. Should be overwritten by
        the implementing class, otherwise will raise `WsgiMethodNotAllowed`.
        """
        raise WsgiMethodNotallowed()

    def GET(self, *args):
        """Method called for an HTTP GET request.

        This method is called if the HTTP REQUEST_METHOD is "GET", unless a
        method routed using `route` is called instead. Should be overwritten by
        the implementing class, otherwise will raise `WsgiMethodNotAllowed`.
        """
        raise WsgiMethodNotallowed()

    def POST(self, *args):
        """Method called for an HTTP POST request.

        This method is called if the HTTP REQUEST_METHOD is "POST", unless a
        method routed using `route` is called instead. Should be overwritten by
        the implementing class, otherwise will raise `WsgiMethodNotAllowed`.
        """
        raise WsgiMethodNotallowed()

    def PUT(self, *args):
        """Method called for an HTTP PUT request.

        This method is called if the HTTP REQUEST_METHOD is "PUT", unless a
        method routed using `route` is called instead. Should be overwritten by
        the implementing class, otherwise will raise `WsgiMethodNotAllowed`.
        """
        raise WsgiMethodNotallowed()

    def before_request(self):
        """Called before request is handed over to a handling method.

        This method is called right after parsing headers and input values,
        after routing is done, but right before the request is passed on to a
        GET, DELETE, PUT, POST or a routed method. By default does nothing.
        """
        pass

    def before_response(self, http_status: str, http_headers: list,
                        response_body: bytes):
        """Called before a handled response is returned to browser.

        This method is called after a request has been fully processed by a
        handling method, right before WSGI start_response method is called, i.e
        before data is given back to the handling web server.

        This method is called *in any case*, even if the handling request
        returns or raises an exception. By default does nothing.
        """
        pass

    def render_template(self, template_name: str, **kwargs: Any) -> Tuple[str, Tuple]:
        """Render a template file and return a WSGI compliant response.

        Loads a template file located in the template folder and renders it as
        a jinja2 template.

        Any keyword arguments are passed straight to the template context and
        are available as local variables within the template. In addition the
        variable "app" is available, which points to the WSGI app itself, i.e:

        * app.form_data
        * app.json
        * app.session

        ..etc are available within the template.

        If keyword argument "headers" is set, it is used as the headers for
        the response instead. If not, the headers will be set to
        Content-Type: text/html.

        Args:
            template_name: Name or path of a template to load
            **kwargs: Variables to pass to the jinja2 template

        Returns:
            A tuple of rendered template content (string) and a list of headers
            (tuple), that can be passed directly as the return value in a WSGI
            app

        Sample usage:
        >>> class WebUi(WsgiApp):
        >>>     template_path = "ui/static/templates/"
        >>>     @WsgiApp.route("start")
        >>>     def start_page(self):
        >>>         return self.render_template("start.html")

        """
        if not self.template_env:
            raise RuntimeError("jinja2 templating is not available. Is it installed?")

        template = self.template_env.get_template(template_name)

        kwargs["app"] = self

        headers = kwargs.get("headers", [])

        header_dict = dict(headers)
        if "Content-Type" not in header_dict:
            headers.append(("Content-Type", "text/html"))

        return template.render(**kwargs), headers


class WsgiAppDispatcher:
    """A callable WSGI application mapper.

    An instance of WsgiAppDispatcher is what should be passed on to a
    WSGI-compliant web server. The web server will call the app dispatcher for
    every incoming request, and the dispatcher will forward the request to an
    appropriate WSGI app, and then returns the return values from the app back
    to the server.
    """

    def __init__(self, pathmap: List[Tuple[str, Type[WsgiApp]]]):
        """Create a new app dispatcher.

        Args:
            pathmap: A list of path-class pairs, where the path is a string that
                may contain regular expressions, which is matched against the
                current request URL path. When a path matches, a new instance of
                the given class is created and executed

        Usage examples:
        >>> class UiApp(WsgiApp):
        >>>     pass
        >>> class ServiceApp(WsgiApp):
        >>>     pass
        >>> class StatusApp(WsgiApp):
        >>>     pass
        >>>
        >>> app = WsgiAppDispatcher([
        >>>     ("/service/(.+)", ServiceApp),
        >>>     ("/status", StatusApp),
        >>>     ("/(.+)", UiApp)
        >>> ])
        """
        self.pathmap = pathmap

    def __call__(self, env, start_response):
        path = env["PATH_INFO"] or "/"
        for p, app in self.pathmap:
            result = None
            try:
                result = re.match("^" + p + "$", path)
            except re.error as e:
                print(f"Path regular expression error: {str(e)} for path {path}")
            if result:
                return app([x for x in result.groups()])(env, start_response)

        start_response("404 Not Found", [("Content-Type", "text/plain"),
                                         ("Content-Length", "0")])
        return [""]


class WsgiHttpResponse:
    """A base class for response states.

    Instead of returning a tuple of (str, headers) as a response to a request,
    an instance of WsgiHttpResponse can be returned instead, serving as a
    shortcut for status code and headers setting.

    Several shortcuts to often used status codes, such as 201, 404 etc exist
    as subclasses of this class, but custom status responses can be created on
    the fly as well:

    >>> return WsgiHttpResponse("This page no longer exists", 302)
    """
    code: Optional[int] = None
    """HTTP status code"""
    description: Optional[str] = None
    """Short description.

    If set, and data is not set, will return a simple HTML page to browser with
    the HTTP status as header and the short description as a single paragraph
    in the body.
    """
    data: Optional[str] = None
    """Alternative response data.

    If set, will be returned to browser as-is.
    """
    headers: Optional[List[Tuple]]
    """A list of headers to send with the response.

    Initially contains just Content-Type: text/html, but can be extended or
    overwritten
    """

    def __init__(self, description=None, code=None, data=None, headers=None):
        # can't initialise this in class definition as it's mutable
        self.headers = [("Content-Type", "text/html")]

        if description:
            self.description = description
        if code:
            self.code = code
        if data:
            self.data = data
        if headers:
            self.headers = headers

    @property
    def html_body(self):
        if self.data:
            return self.data

        return "<html><h1>{} {}</h1><p>{}</p></html".format(
            self.code, self.name, self.description or "")

    @property
    def http_status(self):
        """Human readable, standard HTTP response status string.

        E.g "200 OK", "40 Not Found"
        """
        return f"{self.code} {self.name}"

    @property
    def name(self):
        """Human readable, standard HTTP response status name.

        E.g "OK", "Not Found"
        """
        return HTTP_STATUS_CODES.get(self.code, "Unknown Error")


class WsgiHttpError(WsgiHttpResponse, Exception):
    """Base class for HTTP errors.

    This extends Exception, so it can be raised, instead of just being returned.

    E.g:
    >>> raise WsgiUnauthorized("You are not authorized to view this page")

    Would result in:
    ```
    <html>
        <h1>401 Unauthorized</h1>
        <p>You are not authorized to view this page"</p>
    </html>
    ```

    Similar to the parent class, custom errors can be created on the fly:
    >>> raise WsgiHttpError("Unsupported filetype", 415)
    """
    pass


class WsgiOk(WsgiHttpResponse):
    """Shorthand for HTTP 200 OK."""
    code = 200


class WsgiCreated(WsgiHttpResponse):
    """Shorthand for HTTP 201 Created."""
    code = 201


class WsgiBadRequest(WsgiHttpError):
    """Shorthand for HTTP 400 Bad Request."""
    code = 400


class WsgiUnauthorized(WsgiHttpError):
    """Shorthand for HTTP 401 Unauthorized."""
    code = 401


class WsgiNotFound(WsgiHttpError):
    """Shorthand for HTTP 404 Not Found."""
    code = 404
    description = "The requested URL was not found on the server"


class WsgiInternalServerError(WsgiHttpError):
    """Shorthand for HTTP 500 Internal Server Error."""
    code = 500
    description = (
        "The server encountered an internal error and was unable to "
        "complete your request.  Either the server is overloaded or there "
        "is an error in the application.")


class WsgiMethodNotallowed(WsgiHttpError):
    """Shorthand for HTTP 405 Method Not Allowed"""
    code = 405
    description = "Invalid method or method not allowed for the requested URL"


def json_response(data: dict = None, additional_headers: List[Tuple] = None,
                  **kwargs: Any) -> Tuple[str, List[Tuple]]:
    """A helper method to produce a JSON response.

    A simple wrapper to produce a WSGI-compliant response, a shortcut to:
    >>> return json.dumps({}), [("Content-Type", "application/json")]

    with some additional bonus features.

    Sample usage:
    >>> return wsgi.json_response(foo="bar", bar="baz")
    >>> return wsgi.json_response({"foo": "bar"})

    Args:
        data: Dictionary or object to convert to JSON, optional
        additional_headers: List of tuples containing additional HTTP headers
        **kwargs: Additional values to add to JSON response

    Returns:
        A tuple with string JSON data and a list of headers as tuples, can be
        passed on as a return value for an WSGI app directly.

    """
    headers = [("Content-Type", "application/json")]
    if additional_headers:
        headers.extend(additional_headers)

    if data is None:
        data = {}
    if isinstance(data, dict):
        data.update(kwargs)

    return json.dumps(data), headers


def parse_qs_dict(qs: str, no_default_list: bool = True) -> dict:
    """Parse a query string that contains nested dictionaries.

    Similar to urlparse.parse_qs, except parses also query strings that contain
    dictionary definitions, such as

        foo=bar&bar[]=1&bar[]=2&baz[y]=3&baz[x]=4

    into a dictionary with properly parsed sub-dictionaries, e.g:

        {'baz': {'y': '3', 'x': '4'}, 'foo': 'bar', 'bar': ['1', '2']}

    Also parses nested dictionaries, such as baz[x][a]=1&baz[x][b]=2.

    Args:
        qs: A well-formed query string
        no_default_list: Do not return lists for parameter values if
            there is only one value present. Setting this to False returns to
            default urlparse behaviour, where every value is a list

    Returns:
        Dictionary

    """
    qs = urllib.parse.parse_qs(qs, keep_blank_values=True)
    result = {}

    for key, value in qs.items():
        if isinstance(value, list) and len(value) == 1 and no_default_list:
            value = value[0]

        elif isinstance(value, list):
            for i, x in enumerate(value):
                if isinstance(x, str):
                    value[i] = x

        if "[" in key:
            sub_keys = re.split(r"[\[\]]+", key)
            sub_keys.remove("")
            sub_result = result

            for sub_key in sub_keys[:-1]:
                if sub_key not in sub_result:
                    sub_result[sub_key] = {}
                sub_result = sub_result[sub_key]
            sub_result[sub_keys[-1]] = value
        else:
            result[key] = value

    return result
