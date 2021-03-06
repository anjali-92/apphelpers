from dataclasses import dataclass, asdict
from functools import wraps
from falcon import HTTPUnauthorized, HTTPForbidden, HTTPNotFound

import hug

from apphelpers.db.peewee import dbtransaction
from apphelpers.errors import InvalidSessionError
from apphelpers.sessions import SessionDBHandler


def phony(f):
    return f


def raise_not_found_on_none(f):
    if getattr(f, 'not_found_on_none', None) == True:
        @wraps(f)
        def wrapper(*a, **k):
            ret = f(*a, **k)
            if ret is None:
                raise HTTPNotFound('four o four')
            return ret
        return wrapper
    return f


@hug.directive()
def user_id(default=None, request=None, **kwargs):
    return request.context['user'].id


@hug.directive()
def user_name(default=None, request=None, **kwargs):
    return request.context['user'].name


@hug.directive()
def user_email(default=None, request=None, **kwargs):
    return request.context['user'].email


@dataclass
class User:
    sid: str=None
    id: int=None
    name: str=None
    groups: tuple=()
    email: str=None

    def to_dict(self):
        return asdict(self)

    def __bool__(self):
        return bool(self.id)


def setup_strict_context_setter(sessions):

    def set_context(token):

        uid, groups, name, email = None, [], '', None

        if token:
            try:
                session = sessions.get(token, ['uid', 'name', 'groups', 'email'])
                uid, name, groups, email = session['uid'], session['name'], session['groups'], session['email']
            except InvalidSessionError:
                raise HTTPUnauthorized('Invalid or expired session')

        return User(sid=token, id=uid, name=name, groups=groups, email=email)

    return set_context


def setup_context_setter(sessions):

    def set_context(response, request, context, module):
        """
        Only sets context based on session.
        Does not raise any error
        """
        uid, groups, name, email = None, [], '', None
        token = request.get_header('Authorization')

        if token:
            try:
                session = sessions.get(token, ['uid', 'name', 'groups', 'email'])
                uid, name, groups, email = session['uid'], session['name'], session['groups'], session['email']
            except InvalidSessionError:
                pass

        request.context['user'] = User(sid=token, id=uid, name=name, groups=groups, email=email)

    return set_context


class APIFactory:

    def __init__(self, router):
        self._router = router
        self.db_tr_wrapper = phony
        self.access_wrapper = phony
        self.secure_router = None

    def setup_db_transaction(self, db):
        self.db_tr_wrapper = dbtransaction(db)

    def setup_session_db(self, sessiondb_conn):
        """
        redis_conn_params: dict() with below keys
                           (host, port, password, db)
        """
        self.sessions = SessionDBHandler(sessiondb_conn)
        set_context = setup_context_setter(self.sessions)
        self.router = self._router.http(requires=set_context)
        set_context = setup_strict_context_setter(self.sessions)
        self.secure_router = self.router.http(requires=hug.authentication.token(set_context))

        def access_wrapper(f):
            """
            This is the authentication + authorization part
            """
            login_required = getattr(f, 'login_required', None)
            groups_required = getattr(f, 'groups_required', None)
            groups_forbidden = getattr(f, 'groups_forbidden', None)

            if login_required or groups_required or groups_forbidden:

                @wraps(f)
                def wrapper(request, *args, **kw):

                    user = request.context['user']

                    # this is authentication part
                    if not user.id:
                        raise HTTPUnauthorized('Invalid or expired session')

                    # this is authorization part
                    groups = set(user.groups)

                    if groups_required and not groups.intersection(groups_required):
                        raise HTTPForbidden('Unauthorized access')

                    if groups_forbidden and groups.intersection(groups_forbidden):
                        raise HTTPForbidden('Unauthorized access')

                    return f(*args, **kw)
            else:

                wrapper = f

            return wrapper

        self.access_wrapper = access_wrapper

    def choose_router(self, f):
        login_required = hasattr(f, 'login_required') and f.login_required
        return self.secure_router if login_required else self.router

    def build(self, method, method_args, method_kw, f):
        print(f'{method_args[0]} [{method.__name__.upper()}] => {f.__module__}:{f.__name__}')
        m = method(*method_args, **method_kw)
        f = raise_not_found_on_none(self.access_wrapper(self.db_tr_wrapper(f)))
        return m(f)

    def get(self, *a, **k):
        def _wrapper(f):
            router = self.choose_router(f)
            return self.build(router.get, a, k, f)
        return _wrapper

    def post(self, *a, **k):
        def _wrapper(f):
            router = self.choose_router(f)
            return self.build(router.post, a, k, f)
        return _wrapper

    def patch(self, *a, **k):
        def _wrapper(f):
            router = self.choose_router(f)
            return self.build(router.patch, a, k, f)
        return _wrapper

    def delete(self, *a, **k):
        def _wrapper(f):
            router = self.choose_router(f)
            return self.build(router.delete, a, k, f)
        return _wrapper

    def map_resource(self, url, resource=None, handlers=None, id_field='id'):
        if resource:
            raise NotImplementedError("Resource not supported yet")

        collection_url = (self.urls_prefix + url) if not url.startswith('/') else url
        resource_url = collection_url + '{' + id_field + '}'

        assert isinstance(handlers, (list, tuple)), "handlers should be list or tuple"
        get_collection, add_resource, replace_resource, get_resource, update_resource, delete_resource = handlers

        if get_collection:
            self.get(collection_url)(get_collection)
        if add_resource:
            self.post(collection_url)(add_resource)
        if replace_resource:
            self.put(resource_url)(replace_resource)
        if get_resource:
            # get_resource_wrapped = get_or_not_found(get_resource)
            self.get(resource_url)(get_resource)
        if update_resource:
            self.patch(resource_url)(update_resource)
        if delete_resource:
            self.delete(resource_url)(delete_resource)
