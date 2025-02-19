import asyncio
import functools
import os
import sys
import warnings
from threading import Lock

import sqlalchemy
from sqlalchemy import event
from sqlalchemy import orm
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm.exc import UnmappedClassError
from sqlalchemy.orm.session import Session as SessionBase
from fastapi.applications import FastAPI
from fastapi.requests import Request

from fastapi_sqlalchemy.model import DefaultMeta
from fastapi_sqlalchemy.model import Model

try:
    from sqlalchemy.orm import declarative_base
    from sqlalchemy.orm import DeclarativeMeta
except ImportError:
    # SQLAlchemy <= 1.3
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.ext.declarative import DeclarativeMeta

from threading import get_ident as _ident_func

__version__ = "3.0.0.dev0"


def _sa_url_set(url, **kwargs):
    try:
        url = url.set(**kwargs)
    except AttributeError:
        # SQLAlchemy <= 1.3
        for key, value in kwargs.items():
            setattr(url, key, value)

    return url


def _sa_url_query_setdefault(url, **kwargs):
    query = dict(url.query)

    for key, value in kwargs.items():
        query.setdefault(key, value)

    return _sa_url_set(url, query=query)


def _make_table(db):
    def _make_table(*args, **kwargs):
        if len(args) > 1 and isinstance(args[1], db.Column):
            args = (args[0], db.metadata) + args[1:]
        info = kwargs.pop("info", None) or {}
        info.setdefault("bind_key", None)
        kwargs["info"] = info
        return sqlalchemy.Table(*args, **kwargs)

    return _make_table


def _set_default_query_class(d, cls):
    if "query_class" not in d:
        d["query_class"] = cls


def _wrap_with_default_query_class(fn, cls):
    @functools.wraps(fn)
    def newfn(*args, **kwargs):
        _set_default_query_class(kwargs, cls)
        if "backref" in kwargs:
            backref = kwargs["backref"]
            if isinstance(backref, str):
                backref = (backref, {})
            _set_default_query_class(backref[1], cls)
        return fn(*args, **kwargs)

    return newfn


def _include_sqlalchemy(obj, cls):
    for module in sqlalchemy, sqlalchemy.orm:
        for key in module.__all__:
            if not hasattr(obj, key):
                setattr(obj, key, getattr(module, key))
    # Note: obj.Table does not attempt to be a SQLAlchemy Table class.
    obj.Table = _make_table(obj)
    obj.relationship = _wrap_with_default_query_class(obj.relationship, cls)
    obj.relation = _wrap_with_default_query_class(obj.relation, cls)
    obj.dynamic_loader = _wrap_with_default_query_class(obj.dynamic_loader, cls)
    obj.event = event


def _calling_context(app_path):
    frm = sys._getframe(1)
    while frm.f_back is not None:
        name = frm.f_globals.get("__name__")
        if name and (name == app_path or name.startswith(f"{app_path}.")):
            funcname = frm.f_code.co_name
            return f"{frm.f_code.co_filename}:{frm.f_lineno} ({funcname})"
        frm = frm.f_back
    return "<unknown>"


class SignallingSession(SessionBase):
    """The signalling session is the default session that Flask-SQLAlchemy
    uses.  It extends the default session system with bind selection and
    modification tracking.

    If you want to use a different session you can override the
    :meth:`SQLAlchemy.create_session` function.

    .. versionadded:: 2.0

    .. versionadded:: 2.1
        The `binds` option was added, which allows a session to be joined
        to an external transaction.
    """

    def __init__(self, db, autocommit=False, autoflush=True, **options):
        #: The application that this session belongs to.
        self.app = app = db.get_app()
        track_modifications = app.state.sa_config["SQLALCHEMY_TRACK_MODIFICATIONS"]
        bind = options.pop("bind", None) or db.engine
        binds = options.pop("binds", db.get_binds(app))

        SessionBase.__init__(
            self,
            autocommit=autocommit,
            autoflush=autoflush,
            bind=bind,
            binds=binds,
            **options,
        )

    def get_bind(self, mapper=None, **kwargs):
        """Return the engine or connection for a given model or
        table, using the ``__bind_key__`` if it is set.
        """
        # mapper is None if someone tries to just get a connection
        if mapper is not None:
            try:
                # SA >= 1.3
                persist_selectable = mapper.persist_selectable
            except AttributeError:
                # SA < 1.3
                persist_selectable = mapper.mapped_table

            info = getattr(persist_selectable, "info", {})
            bind_key = info.get("bind_key")
            if bind_key is not None:
                state = get_state(self.app)
                return state.db.get_engine(self.app, bind=bind_key)

        return super().get_bind(mapper, **kwargs)


class BaseQuery(orm.Query):
    """SQLAlchemy :class:`~sqlalchemy.orm.query.Query` subclass with
    convenience methods for querying in a web application.

    This is the default :attr:`~Model.query` object used for models, and
    exposed as :attr:`~SQLAlchemy.Query`. Override the query class for
    an individual model by subclassing this and setting
    :attr:`~Model.query_class`.
    """


class _QueryProperty:
    def __init__(self, sa):
        self.sa = sa

    def __get__(self, obj, type):
        try:
            mapper = orm.class_mapper(type)
            if mapper:
                return type.query_class(mapper, session=self.sa.session())
        except UnmappedClassError:
            return None


def _record_queries(app):
    if app.debug:
        return True
    rq = app.state.sa_config["SQLALCHEMY_RECORD_QUERIES"]
    if rq is not None:
        return rq
    return bool(app.state.sa_config.get("TESTING"))


class _EngineConnector:
    def __init__(self, sa, app, bind=None):
        self._sa = sa
        self._app = app
        self._engine = None
        self._connected_for = None
        self._bind = bind
        self._lock = Lock()

    def get_uri(self) -> str:
        if self._bind is None:
            return self._app.state.sa_config["SQLALCHEMY_DATABASE_URI"]
        binds = self._app.state.sa_config.get("SQLALCHEMY_BINDS") or ()
        assert (
            self._bind in binds
        ), f"Bind {self._bind!r} is not configured in 'SQLALCHEMY_BINDS'."
        return binds[self._bind]

    def get_engine(self) -> sqlalchemy.engine.Engine:
        with self._lock:
            uri = self.get_uri()
            echo = self._app.state.sa_config["SQLALCHEMY_ECHO"]
            if (uri, echo) == self._connected_for:
                return self._engine

            sa_url = make_url(uri)
            sa_url, options = self.get_options(sa_url, echo)
            self._engine = rv = self._sa.create_engine(sa_url, options)

            self._connected_for = (uri, echo)

            return rv

    def get_options(self, sa_url, echo):
        options = {}
        sa_url, options = self._sa.apply_driver_hacks(self._app, sa_url, options)

        if echo:
            options["echo"] = echo

        # Give the config options set by a developer explicitly priority
        # over decisions FSA makes.
        options.update(self._app.state.sa_config["SQLALCHEMY_ENGINE_OPTIONS"])
        # Give options set in SQLAlchemy.__init__() ultimate priority
        options.update(self._sa._engine_options)
        return sa_url, options


def get_state(app):
    """Gets the state for the application"""
    try:
        return app.state.sqlalchemy
    except AttributeError:
        raise AssertionError(
            "The sqlalchemy extension was not registered to the current "
            "application.  Please make sure to call init_app() first."
        )


class _SQLAlchemyState:
    """Remembers configuration for the (db, app) tuple."""

    def __init__(self, db):
        self.db = db
        self.connectors = {}


class SQLAlchemy:
    """This class is used to control the SQLAlchemy integration to one
    or more Flask applications.  Depending on how you initialize the
    object it is usable right away or will attach as needed to a
    Flask application.

    There are two usage modes which work very similarly.  One is binding
    the instance to a very specific Flask application::

        app = Flask(__name__)
        db = SQLAlchemy(app)

    The second possibility is to create the object once and configure the
    application later to support it::

        db = SQLAlchemy()

        def create_app():
            app = Flask(__name__)
            db.init_app(app)
            return app

    The difference between the two is that in the first case methods like
    :meth:`create_all` and :meth:`drop_all` will work all the time but in
    the second case a :meth:`flask.Flask.app_context` has to exist.

    By default Flask-SQLAlchemy will apply some backend-specific settings
    to improve your experience with them.

    This class also provides access to all the SQLAlchemy functions and classes
    from the :mod:`sqlalchemy` and :mod:`sqlalchemy.orm` modules.  So you can
    declare models like this::

        class User(db.Model):
            username = db.Column(db.String(80), unique=True)
            pw_hash = db.Column(db.String(80))

    You can still use :mod:`sqlalchemy` and :mod:`sqlalchemy.orm` directly, but
    note that Flask-SQLAlchemy customizations are available only through an
    instance of this :class:`SQLAlchemy` class.  Query classes default to
    :class:`BaseQuery` for `db.Query`, `db.Model.query_class`, and the default
    query_class for `db.relationship` and `db.backref`.  If you use these
    interfaces through :mod:`sqlalchemy` and :mod:`sqlalchemy.orm` directly,
    the default query class will be that of :mod:`sqlalchemy`.

    .. admonition:: Check types carefully

       Don't perform type or `isinstance` checks against `db.Table`, which
       emulates `Table` behavior but is not a class. `db.Table` exposes the
       `Table` interface, but is a function which allows omission of metadata.

    The ``session_options`` parameter, if provided, is a dict of parameters
    to be passed to the session constructor. See
    :class:`~sqlalchemy.orm.session.Session` for the standard options.

    The ``engine_options`` parameter, if provided, is a dict of parameters
    to be passed to create engine.  See :func:`~sqlalchemy.create_engine`
    for the standard options.  The values given here will be merged with and
    override anything set in the ``'SQLALCHEMY_ENGINE_OPTIONS'`` config
    variable or othewise set by this library.

    .. versionchanged:: 3.0
        Removed the ``use_native_unicode`` parameter and config.

    .. versionchanged:: 3.0
        ``COMMIT_ON_TEARDOWN`` is deprecated and will be removed in
        version 3.1. Call ``db.session.commit()`` directly instead.

    .. versionchanged:: 2.4
        Added the ``engine_options`` parameter.

    .. versionchanged:: 2.1
        Added the ``metadata`` parameter. This allows for setting custom
        naming conventions among other, non-trivial things.

    .. versionchanged:: 2.1
        Added the ``query_class`` parameter, to allow customisation
        of the query class, in place of the default of
        :class:`BaseQuery`.

    .. versionchanged:: 2.1
        Added the ``model_class`` parameter, which allows a custom model
        class to be used in place of :class:`Model`.

    .. versionchanged:: 2.1
        Use the same query class across ``session``, ``Model.query`` and
        ``Query``.

    .. versionchanged:: 0.16
        ``scopefunc`` is now accepted on ``session_options``. It allows
        specifying a custom function which will define the SQLAlchemy
        session's scoping.

    .. versionchanged:: 0.10
        Added the ``session_options`` parameter.
    """

    #: Default query class used by :attr:`Model.query` and other queries.
    #: Customize this by passing ``query_class`` to :func:`SQLAlchemy`.
    #: Defaults to :class:`BaseQuery`.
    Query = None
    session: SessionBase

    def __init__(
        self,
        app=None,
        session_options=None,
        metadata=None,
        query_class=BaseQuery,
        model_class=Model,
        engine_options=None,
    ):

        self.Query = query_class
        self.session: orm.scoped_session = self.create_scoped_session(session_options)
        self.Model: declarative_base = self.make_declarative_base(model_class, metadata)
        self._engine_lock = Lock()
        self.app = app
        self.config = {}
        self._engine_options = engine_options or {}
        _include_sqlalchemy(self, query_class)

    @property
    def metadata(self):
        """The metadata associated with ``db.Model``."""

        return self.Model.metadata

    def create_scoped_session(self, options=None) -> orm.scoped_session:
        """Create a :class:`~sqlalchemy.orm.scoping.scoped_session`
        on the factory from :meth:`create_session`.

        An extra key ``'scopefunc'`` can be set on the ``options`` dict to
        specify a custom scope function.  If it's not provided, Flask's app
        context stack identity is used. This will ensure that sessions are
        created and removed with the request/response cycle, and should be fine
        in most cases.

        :param options: dict of keyword arguments passed to session class  in
            ``create_session``
        """

        if options is None:
            options = {}

        scopefunc = options.pop("scopefunc", _ident_func)
        options.setdefault("query_cls", self.Query)
        return orm.scoped_session(self.create_session(options), scopefunc=scopefunc)

    def create_session(self, options):
        """Create the session factory used by :meth:`create_scoped_session`.

        The factory **must** return an object that SQLAlchemy recognizes as a session,
        or registering session events may raise an exception.

        Valid factories include a :class:`~sqlalchemy.orm.session.Session`
        class or a :class:`~sqlalchemy.orm.session.sessionmaker`.

        The default implementation creates a ``sessionmaker`` for
        :class:`SignallingSession`.

        :param options: dict of keyword arguments passed to session class
        """

        return orm.sessionmaker(class_=SignallingSession, db=self, **options)

    def make_declarative_base(self, model, metadata=None) -> sqlalchemy.orm.DeclarativeMeta:
        """Creates the declarative base that all models will inherit from.

        :param model: base model class (or a tuple of base classes) to pass
            to :func:`~sqlalchemy.ext.declarative.declarative_base`. Or a class
            returned from ``declarative_base``, in which case a new base class
            is not created.
        :param metadata: :class:`~sqlalchemy.MetaData` instance to use, or
            none to use SQLAlchemy's default.

        .. versionchanged 2.3.0::
            ``model`` can be an existing declarative base in order to support
            complex customization such as changing the metaclass.
        """
        if not isinstance(model, DeclarativeMeta):
            model = declarative_base(
                cls=model, name="Model", metadata=metadata, metaclass=DefaultMeta
            )

        # if user passed in a declarative base and a metaclass for some reason,
        # make sure the base uses the metaclass
        if metadata is not None and model.metadata is not metadata:
            model.metadata = metadata

        if not getattr(model, "query_class", None):
            model.query_class = self.Query

        model.query = _QueryProperty(self)
        return model

    def init_app(self, app: FastAPI, config: dict):
        """This callback can be used to initialize an application for the
        use with this database setup.  Never use a database in the context
        of an application not initialized that way or connections will
        leak.
        """

        def setdefault(_config, _key, _value):
            if _key not in _config:
                _config[_key] = _value

        # We intentionally don't set self.app = app, to support multiple
        # applications. If the app is passed in the constructor,
        # we set it and don't support multiple applications.
        if not (
            config.get("SQLALCHEMY_DATABASE_URI", None)
            or config.get("SQLALCHEMY_BINDS", None)
        ):
            raise RuntimeError(
                "Either SQLALCHEMY_DATABASE_URI or SQLALCHEMY_BINDS needs to be set."
            )

        setdefault(config, "SQLALCHEMY_DATABASE_URI", None)
        setdefault(config, "SQLALCHEMY_BINDS", None)
        setdefault(config, "SQLALCHEMY_ECHO", False)
        setdefault(config, "SQLALCHEMY_RECORD_QUERIES", None)
        setdefault(config, "SQLALCHEMY_COMMIT_ON_TEARDOWN", False)
        setdefault(config, "SQLALCHEMY_TRACK_MODIFICATIONS", False)
        setdefault(config, "SQLALCHEMY_ENGINE_OPTIONS", {})

        self.app: FastAPI = app
        self.config: dict = config
        app.state.sqlalchemy = _SQLAlchemyState(self)
        app.state.sa_config = config

        @app.middleware("http")
        async def db_session_middleware(request: Request, call_next):
            try:
                self.session.rollback()
                self.session.flush()
                self.session.expire_all()
                response = await call_next(request)
                if config["SQLALCHEMY_COMMIT_ON_TEARDOWN"]:
                    warnings.warn(
                        "'COMMIT_ON_TEARDOWN' is deprecated and will be"
                        " removed in version 3.1. Call"
                        " 'db.session.commit()'` directly instead.",
                        DeprecationWarning,
                    )

                    self.session.commit()
                return response
            finally:
                self.session.remove()

    def apply_driver_hacks(self, app: FastAPI, sa_url, options):
        """This method is called before engine creation and used to inject
        driver specific hacks into the options.  The `options` parameter is
        a dictionary of keyword arguments that will then be used to call
        the :func:`sqlalchemy.create_engine` function.

        The default implementation provides some defaults for things
        like pool sizes for MySQL and SQLite.

        .. versionchanged:: 3.0
            Change the default MySQL character set to "utf8mb4".

        .. versionchanged:: 2.5
            Returns ``(sa_url, options)``. SQLAlchemy 1.4 made the URL
            immutable, so any changes to it must now be passed back up
            to the original caller.
        """
        if sa_url.drivername.startswith("mysql"):
            sa_url = _sa_url_query_setdefault(sa_url, charset="utf8mb4")

            if sa_url.drivername != "mysql+gaerdbms":
                options.setdefault("pool_size", 10)
                options.setdefault("pool_recycle", 7200)
        elif sa_url.drivername == "sqlite":
            pool_size = options.get("pool_size")
            detected_in_memory = False
            if sa_url.database in (None, "", ":memory:"):
                detected_in_memory = True
                from sqlalchemy.pool import StaticPool

                options["poolclass"] = StaticPool
                if "connect_args" not in options:
                    options["connect_args"] = {}
                options["connect_args"]["check_same_thread"] = False

                # we go to memory and the pool size was explicitly set
                # to 0 which is fail.  Let the user know that
                if pool_size == 0:
                    raise RuntimeError(
                        "SQLite in memory database with an "
                        "empty queue not possible due to data "
                        "loss."
                    )
            # if pool size is None or explicitly set to 0 we assume the
            # user did not want a queue for this sqlite connection and
            # hook in the null pool.
            elif not pool_size:
                from sqlalchemy.pool import NullPool

                options["poolclass"] = NullPool

            # If the database path is not absolute, it's relative to the
            # app instance path, which might need to be created.
            if not detected_in_memory and not os.path.isabs(sa_url.database):
                root_path = app.root_path or '.'
                os.makedirs(root_path, exist_ok=True)
                sa_url = _sa_url_set(
                    sa_url, database=os.path.join(root_path, sa_url.database)
                )

        return sa_url, options

    @property
    def engine(self) -> sqlalchemy.engine.Engine:
        """Gives access to the engine.  If the database configuration is bound
        to a specific application (initialized with an application) this will
        always return a database connection.  If however the current application
        is used this might raise a :exc:`RuntimeError` if no application is
        active at the moment.
        """
        return self.get_engine()

    def make_connector(self, app=None, bind=None) -> _EngineConnector:
        """Creates the connector for a given state and bind."""
        return _EngineConnector(self, self.get_app(app), bind)

    def get_engine(self, app=None, bind=None) -> sqlalchemy.engine.Engine:
        """Returns a specific engine."""

        app = self.get_app(app)
        state = get_state(app)

        with self._engine_lock:
            connector = state.connectors.get(bind)

            if connector is None:
                connector = self.make_connector(app, bind)
                state.connectors[bind] = connector

            return connector.get_engine()

    def create_engine(self, sa_url, engine_opts) -> sqlalchemy.engine.Engine:
        """Override this method to have final say over how the
        SQLAlchemy engine is created.

        In most cases, you will want to use
        ``'SQLALCHEMY_ENGINE_OPTIONS'`` config variable or set
        ``engine_options`` for :func:`SQLAlchemy`.
        """
        return sqlalchemy.create_engine(sa_url, **engine_opts)

    def get_app(self, reference_app=None) -> FastAPI:
        """Helper method that implements the logic to look up an
        application."""

        if reference_app is not None:
            return reference_app

        if self.app is not None:
            return self.app

        raise RuntimeError(
            "No application found. Either work inside a view function or push"
            " an application context. See"
            " https://flask-sqlalchemy.palletsprojects.com/contexts/."
        )

    def get_tables_for_bind(self, bind=None):
        """Returns a list of all tables relevant for a bind."""
        result = []
        for table in self.Model.metadata.tables.values():
            if table.info.get("bind_key") == bind:
                result.append(table)
        return result

    def get_binds(self, app=None):
        """Returns a dictionary with a table->engine mapping.

        This is suitable for use of sessionmaker(binds=db.get_binds(app)).
        """
        app = self.get_app(app)
        binds = [None] + list(self.config.get("SQLALCHEMY_BINDS") or ())
        retval = {}
        for bind in binds:
            engine = self.get_engine(app, bind)
            tables = self.get_tables_for_bind(bind)
            retval.update({table: engine for table in tables})
        return retval

    def _execute_for_all_tables(self, app, bind, operation, skip_tables=False):
        app = self.get_app(app)

        if bind == "__all__":
            binds = [None] + list(self.config.get("SQLALCHEMY_BINDS") or ())
        elif isinstance(bind, str) or bind is None:
            binds = [bind]
        else:
            binds = bind

        for bind in binds:
            extra = {}
            if not skip_tables:
                tables = self.get_tables_for_bind(bind)
                extra["tables"] = tables
            op = getattr(self.Model.metadata, operation)
            op(bind=self.get_engine(app, bind), **extra)

    def create_all(self, bind="__all__", app=None):
        """Create all tables that do not already exist in the database.
        This does not update existing tables, use a migration library
        for that.

        :param bind: A bind key or list of keys to create the tables
            for. Defaults to all binds.
        :param app: Use this app instead of requiring an app context.

        .. versionchanged:: 0.12
            Added the ``bind`` and ``app`` parameters.
        """
        self._execute_for_all_tables(app, bind, "create_all")

    def drop_all(self, bind="__all__", app=None):
        """Drop all tables.

        :param bind: A bind key or list of keys to drop the tables for.
            Defaults to all binds.
        :param app: Use this app instead of requiring an app context.

        .. versionchanged:: 0.12
            Added the ``bind`` and ``app`` parameters.
        """
        self._execute_for_all_tables(app, bind, "drop_all")

    def reflect(self, bind="__all__", app=None):
        """Reflects tables from the database.

        :param bind: A bind key or list of keys to reflect the tables
            from. Defaults to all binds.
        :param app: Use this app instead of requiring an app context.

        .. versionchanged:: 0.12
            Added the ``bind`` and ``app`` parameters.
        """
        self._execute_for_all_tables(app, bind, "reflect", skip_tables=True)

    def __repr__(self):
        url = self.engine.url if self.app else None
        return f"<{type(self).__name__} engine={url!r}>"
