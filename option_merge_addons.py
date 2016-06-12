from input_algorithms.errors import BadSpecValue
from input_algorithms.dictobj import dictobj
from input_algorithms import spec_base as sb
from input_algorithms import validators
from input_algorithms.meta import Meta

from delfick_error import DelfickError, ProgrammerError
from collections import defaultdict
from layerz import Layers
import pkg_resources
import logging
import six

log = logging.getLogger("option_merge.addons")

class option_merge_addon_hook(object):
    def __init__(self, extras=sb.NotSpecified, post_register=False):
        self.post_register = post_register
        if post_register and extras not in (None, {}, sb.NotSpecified):
            msg = "Sorry, can't specify ``extras`` and ``post_register`` at the same time"
            raise ProgrammerError(msg)
        spec = sb.listof(sb.tuple_spec(sb.string_spec(), sb.listof(sb.string_spec())))
        self.extras = spec.normalise(Meta({}, []), extras)

    def __call__(self, func):
        func.extras = self.extras
        func._option_merge_addon_entry = True
        func._option_merge_addon_entry_post_register = self.post_register
        return func

class spec_key_spec(sb.Spec):
    """
    Turns value into (int, (str1, str2, ..., strn))

    If value is a single string: (0, (val, ))

    if value is a tuple of strings: (0, (val1, val2, ..., valn))

    if value is a list of strings: (0, (val2, val2, ..., valn))

    if value is already correct, then return as is
    """
    def normalise_filled(self, meta, val):
        if isinstance(val, six.string_types):
            return (0, (val, ))
        else:
            if isinstance(val, list) or isinstance(val, tuple) and len(val) > 0:
                is_int = type(val[0]) is int
                is_digit = getattr(val[0], "isdigit", lambda: False)()
                if not is_int and not is_digit:
                    val = (0, val)

            spec = sb.tuple_spec(sb.integer_spec(), sb.tupleof(sb.string_spec()))
            return spec.normalise(meta, val)

class no_such_key_spec(sb.Spec):
    def setup(self, reason):
        self.reason = reason

    def normalise_filled(self, meta, val):
        raise BadSpecValue(self.reason, meta=meta)

class Result(dictobj.Spec):
    specs = dictobj.Field(sb.dictof(spec_key_spec(), sb.has("normalise")))
    extra = dictobj.Field(no_such_key_spec("Use extras instead (notice the s!)"))
    extras = dictobj.Field(sb.listof(sb.tuple_spec(sb.string_spec(), sb.tupleof(sb.string_spec()))))

class Addon(dictobj.Spec):
    name = dictobj.Field(sb.string_spec)
    extras = dictobj.Field(sb.listof(sb.tuple_spec(sb.string_spec(), sb.string_spec())))
    resolver = dictobj.Field(sb.any_spec)
    namespace = dictobj.Field(sb.string_spec)

    class BadHook(DelfickError):
        desc = "Bad Hook"

    @property
    def resolved(self):
        errors = []
        if getattr(self, "_resolved", None) is None:
            try:
                self._resolved = list(self.resolver())
            except Exception as error:
                errors.append(self.BadHook("Failed to resolve a hook", name=self.name, namespace=self.namespace, error=str(error)))

        if errors:
            raise self.BadHook(_errors=errors)

        return self._resolved

    def process(self, collector):
        for result in self.resolved:
            collector.register_converters(
                  result.get("specs", {})
                , Meta, collector.configuration, sb.NotSpecified
                )

    def post_register(self, **kwargs):
        list(self.resolver(post_register=True, **kwargs))

    def unresolved_dependencies(self):
        for namespace, name in self.extras:
                yield (namespace, name)

    def resolved_dependencies(self):
        for result in self.resolved:
            for namespace, names in result.get("extras", []):
                if not isinstance(names, (tuple, list)):
                    names = (names, )
                for name in names:
                    yield (namespace, name)

    def dependencies(self, all_deps):
        for dep in self.unresolved_dependencies():
            yield dep
        if hasattr(self, "_resolved"):
            for dep in self.resolved_dependencies():
                yield dep

class AddonGetter(object):
    class NoSuchAddon(DelfickError):
        desc = "No such addon"
    class BadImport(DelfickError):
        desc = "Bad import"
    class BadAddon(DelfickError):
        desc = "Bad addon"

    def __init__(self):
        self.namespaces = {}
        self.add_namespace("option_merge.addons")

    def add_namespace(self, namespace, result_spec=None, addon_spec=None):
        self.namespaces[namespace] = (result_spec or Result.FieldSpec(), addon_spec or Addon.FieldSpec())

    def __call__(self, namespace, entry_point_name, collector):
        if namespace not in self.namespaces:
            log.warning("Unknown plugin namespace\tnamespace=%s\tentry_point=%s\tavailable=%s"
                , namespace, entry_point_name, sorted(self.namespaces.keys())
                )
            return

        entry_point_full_name = "{0}.{1}".format(namespace, entry_point_name)

        entry_points = self.find_entry_points(
              namespace, entry_point_name, entry_point_full_name
            )

        def result_maker(**data):
            return self.namespaces[namespace][0].normalise(Meta(data, []), data)

        resolver, extras = self.resolve_entry_points(
                namespace, entry_point_name, collector
              , result_maker, entry_points, entry_point_full_name
              )

        return self.namespaces[namespace][1].normalise(Meta({}, [])
            , { "namespace": namespace
              , "name": entry_point_name
              , "resolver": resolver
              , "extras": extras
              }
            )

    def find_entry_points(self, namespace, entry_point_name, entry_point_full_name):
        it = pkg_resources.iter_entry_points(namespace, name=entry_point_name)
        entry_points = list(it)

        if len(entry_points) > 1:
            log.warning("Found multiple entry_points for {0}".format(
              entry_point_full_name
            ))
        elif len(entry_points) == 0:
            raise self.NoSuchAddon(addon=entry_point_full_name)
        else:
            log.info("Found {0} addon".format(entry_point_full_name))

        return entry_points

    def resolve_entry_points(self
        , namespace, entry_point_name, collector
        , result_maker, entry_points, entry_point_full_name
        ):
        errors = []
        modules = []
        for entry_point in entry_points:
            try:
                modules.append(entry_point.resolve())
            except ImportError as error:
                err = self.BadImport("Error whilst resolving entry_point"
                    , importing=entry_point_full_name
                    , module=entry_point.module_name
                    , error=str(error)
                    )
                errors.append(err)

        if errors:
            raise self.BadImport("Failed to import some entry points"
                , _errors=errors
                )

        hooks, extras = self.get_hooks_and_extras(modules)
        resolver = self.get_resolver(collector, result_maker, hooks)
        return resolver, extras

    def get_hooks_and_extras(self, modules):
        found = []
        extras = []
        for module in modules:
            for attr in dir(module):
                hook = getattr(module, attr)
                if getattr(hook, "_option_merge_addon_entry", False):
                    found.append(hook)
                    for namespace, names in hook.extras:
                        for name in names:
                            pair = (namespace, name)
                            if pair not in extras:
                                extras.append(pair)
        return found, extras

    def get_resolver(self, collector, result_maker, hooks):
        def resolve(post_register=False, **kwargs):
            for hook in hooks:
                is_post_register = getattr(hook, "_option_merge_addon_entry_post_register", False)
                if (post_register and not is_post_register) or (is_post_register and not post_register):
                    continue

                if post_register:
                    hook(collector, **kwargs)
                else:
                    r = hook(collector, result_maker)
                    if r is not None:
                        yield r

        return resolve

class Register(object):
    """
    Responsible for finding and registering addons.

    Addons can register unresolved dependencies and resolved dependencies.

    The difference is that an unresolved dependency does not involve executing
    the addon, whereas a resolved dependency does.

    Order is such that:
        * import known pairs
        * import extra pairs from known pairs
        * resolve known and extra pairs in layers
        * import and resolve extra pairs from those layers until no more are known
        * call post_register on all pairs in layers

    Usage:

    .. code-block:: python

        register = Register(AddonGetter, collector)

        # Add pairs as many times as you want
        register.add_pairs(("namespace1", "name1"), ("namespace2", "name2"), ..., )
        register.add_pairs(("namespace1", "name1"), ("namespace2", "name2"), ..., )

        # Now we import but not resolve the addons to get the unresolved extras
        register.recursive_import_known()

        # We now have a record of all the unresolved extras to be imported
        # Let's actually call our addons
        # And in the process, import and resolve any resolved extras
        register.recursive_resolve_imported()

        # Finally, everything has been imported and resolved, let's call post_register
        register.post_register({namespace1: {arg1=val1, arg2=val2}, ...})

    Alternatively if you don't want that much control:

    .. code-block:: python

        register = Register(AddonGetter, collector)
        register.register((namespace1, name1), (namespace2, name2), ...
            , namespace1={arg1:val1}, namespace2 = {arg1=val1}
            )

        # This will ensure the same resolution path as the manual approach
    """
    def __init__(self, addon_getter, collector):
        self.known = []
        self.imported = {}
        self.resolved = {}
        self.collector = collector
        self.addon_getter = addon_getter

    ########################
    ###   AUTO USAGE
    ########################

    def register(self, *pairs, **extra_args):
        self.add_pairs(*pairs)
        self.recursive_import_known()
        self.recursive_resolve_imported()
        self.post_register(extra_args)

    ########################
    ###   MANUAL USAGE
    ########################

    def add_pairs(self, *pairs):
        for pair in pairs:
            if pair not in self.known:
                self.known.append(pair)

    def recursive_import_known(self):
        added = False
        while True:
            nxt = self._import_known()
            if not nxt:
                break
            added = nxt or added
        return added

    def recursive_resolve_imported(self):
        while True:
            if not self._resolve_imported():
                break

    def post_register(self, extra_args=None):
        for layer in self.layered:
            for pair, imported in layer:
                args = (extra_args or {}).get(pair[0], {})
                imported.post_register(**args)

    ########################
    ###   LAYERED
    ########################

    @property
    def layered(self):
        layers = Layers(self.imported)
        for key in sorted(self.imported):
            layers.add_to_layers(key)
        for layer in layers.layered:
            yield layer

    ########################
    ###   HELPERS
    ########################

    def _import_known(self):
        added = False
        for pair in list(self.known):
            namespace, name = pair
            if pair not in self.imported:
                imported = self.addon_getter(namespace, name, self.collector)
                if imported is None:
                    self.known.pop(self.known.index(pair))
                else:
                    self.imported[pair] = imported
                    for pair in self.pairs_from_extras(imported.extras):
                        if pair not in self.known:
                            self.known.add(pair)
                    added = True
        return added

    def _resolve_imported(self):
        for layer in self.layered:
            for pair, imported in layer:
                namespace, name = pair
                if pair not in self.resolved:
                    resolved = self.resolved[pair] = list(imported.resolved)
                    imported.process(self.collector)
                    for result in imported.resolved:
                        self.add_pairs(*list(self.pairs_from_extras(result.extras)))
        return self.recursive_import_known()

    def pairs_from_extras(self, extras):
        for pair in extras:
            namespace, names = pair
            if not isinstance(names, (tuple, list)):
                names = (names, )

            for name in names:
                pair = (namespace, name)
                if pair not in self.known:
                    self.known.append(pair)
                    yield pair
