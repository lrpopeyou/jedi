from abc import abstractproperty

from jedi import debug
from jedi import settings
from jedi.evaluate import compiled
from jedi.evaluate.compiled.context import CompiledObjectFilter
from jedi.evaluate.helpers import contexts_from_qualified_names
from jedi.evaluate.filters import AbstractFilter
from jedi.evaluate.names import ContextName, TreeNameDefinition
from jedi.evaluate.base_context import Context, NO_CONTEXTS, ContextSet, \
    iterator_to_context_set, ContextWrapper
from jedi.evaluate.lazy_context import LazyKnownContext, LazyKnownContexts
from jedi.evaluate.cache import evaluator_method_cache
from jedi.evaluate.arguments import AnonymousArguments, \
    ValuesArguments, TreeArgumentsWrapper
from jedi.evaluate.context.function import \
    FunctionContext, FunctionMixin, OverloadedFunctionContext
from jedi.evaluate.context.klass import ClassContext, apply_py__get__, \
    ClassFilter
from jedi.evaluate.context import iterable
from jedi.parser_utils import get_parent_scope


class InstanceExecutedParam(object):
    def __init__(self, instance, tree_param):
        self._instance = instance
        self._tree_param = tree_param
        self.string_name = self._tree_param.name.value

    def infer(self):
        return ContextSet([self._instance])

    def matches_signature(self):
        return True


class AnonymousInstanceArguments(AnonymousArguments):
    def __init__(self, instance):
        self._instance = instance

    def get_executed_params_and_issues(self, execution_context):
        from jedi.evaluate.dynamic import search_params
        tree_params = execution_context.tree_node.get_params()
        if not tree_params:
            return [], []

        self_param = InstanceExecutedParam(self._instance, tree_params[0])
        if len(tree_params) == 1:
            # If the only param is self, we don't need to try to find
            # executions of this function, we have all the params already.
            return [self_param], []
        executed_params = list(search_params(
            execution_context.evaluator,
            execution_context,
            execution_context.tree_node
        ))
        executed_params[0] = self_param
        return executed_params, []


class AbstractInstanceContext(Context):
    """
    This class is used to evaluate instances.
    """
    api_type = u'instance'

    def __init__(self, evaluator, parent_context, class_context, var_args):
        super(AbstractInstanceContext, self).__init__(evaluator, parent_context)
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.class_context = class_context
        self.var_args = var_args

    def is_instance(self):
        return True

    def get_qualified_names(self):
        return self.class_context.get_qualified_names()

    def get_annotated_class_object(self):
        return self.class_context  # This is the default.

    def py__call__(self, arguments):
        names = self.get_function_slot_names(u'__call__')
        if not names:
            # Means the Instance is not callable.
            return super(AbstractInstanceContext, self).py__call__(arguments)

        return ContextSet.from_sets(name.infer().execute(arguments) for name in names)

    def py__class__(self):
        return self.class_context

    def py__bool__(self):
        # Signalize that we don't know about the bool type.
        return None

    def get_function_slot_names(self, name):
        # Python classes don't look at the dictionary of the instance when
        # looking up `__call__`. This is something that has to do with Python's
        # internal slot system (note: not __slots__, but C slots).
        for filter in self.get_filters(include_self_names=False):
            names = filter.get(name)
            if names:
                return names
        return []

    def execute_function_slots(self, names, *evaluated_args):
        return ContextSet.from_sets(
            name.infer().execute_evaluated(*evaluated_args)
            for name in names
        )

    def py__get__(self, obj, class_context):
        """
        obj may be None.
        """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        names = self.get_function_slot_names(u'__get__')
        if names:
            if obj is None:
                obj = compiled.builtin_from_name(self.evaluator, u'None')
            return self.execute_function_slots(names, obj, class_context)
        else:
            return ContextSet([self])

    def get_filters(self, search_global=None, until_position=None,
                    origin_scope=None, include_self_names=True):
        class_context = self.get_annotated_class_object()
        if include_self_names:
            for cls in class_context.py__mro__():
                if not isinstance(cls, compiled.CompiledObject) \
                        or cls.tree_node is not None:
                    # In this case we're excluding compiled objects that are
                    # not fake objects. It doesn't make sense for normal
                    # compiled objects to search for self variables.
                    yield SelfAttributeFilter(self.evaluator, self, cls, origin_scope)

        class_filters = class_context.get_filters(
            search_global=False,
            origin_scope=origin_scope,
            is_instance=True,
        )
        for f in class_filters:
            if isinstance(f, ClassFilter):
                yield InstanceClassFilter(self.evaluator, self, f)
            elif isinstance(f, CompiledObjectFilter):
                yield CompiledInstanceClassFilter(self.evaluator, self, f)
            else:
                # Propably from the metaclass.
                yield f

    def py__getitem__(self, index_context_set, contextualized_node):
        names = self.get_function_slot_names(u'__getitem__')
        if not names:
            return super(AbstractInstanceContext, self).py__getitem__(
                index_context_set,
                contextualized_node,
            )

        args = ValuesArguments([index_context_set])
        return ContextSet.from_sets(name.infer().execute(args) for name in names)

    def py__iter__(self, contextualized_node=None):
        iter_slot_names = self.get_function_slot_names(u'__iter__')
        if not iter_slot_names:
            return super(AbstractInstanceContext, self).py__iter__(contextualized_node)

        def iterate():
            for generator in self.execute_function_slots(iter_slot_names):
                if generator.is_instance() and not generator.is_compiled():
                    # `__next__` logic.
                    if self.evaluator.environment.version_info.major == 2:
                        name = u'next'
                    else:
                        name = u'__next__'
                    next_slot_names = generator.get_function_slot_names(name)
                    if next_slot_names:
                        yield LazyKnownContexts(
                            generator.execute_function_slots(next_slot_names)
                        )
                    else:
                        debug.warning('Instance has no __next__ function in %s.', generator)
                else:
                    for lazy_context in generator.py__iter__():
                        yield lazy_context
        return iterate()

    @abstractproperty
    def name(self):
        pass

    def create_init_executions(self):
        for name in self.get_function_slot_names(u'__init__'):
            # TODO is this correct? I think we need to check for functions.
            if isinstance(name, LazyInstanceClassName):
                function = FunctionContext.from_context(
                    self.parent_context,
                    name.tree_name.parent
                )
                bound_method = BoundMethod(self, function)
                yield bound_method.get_function_execution(self.var_args)

    @evaluator_method_cache()
    def create_instance_context(self, class_context, node):
        if node.parent.type in ('funcdef', 'classdef'):
            node = node.parent
        scope = get_parent_scope(node)
        if scope == class_context.tree_node:
            return class_context
        else:
            parent_context = self.create_instance_context(class_context, scope)
            if scope.type == 'funcdef':
                func = FunctionContext.from_context(
                    parent_context,
                    scope,
                )
                bound_method = BoundMethod(self, func)
                if scope.name.value == '__init__' and parent_context == class_context:
                    return bound_method.get_function_execution(self.var_args)
                else:
                    return bound_method.get_function_execution()
            elif scope.type == 'classdef':
                class_context = ClassContext(self.evaluator, parent_context, scope)
                return class_context
            elif scope.type in ('comp_for', 'sync_comp_for'):
                # Comprehensions currently don't have a special scope in Jedi.
                return self.create_instance_context(class_context, scope)
            else:
                raise NotImplementedError
        return class_context

    def get_signatures(self):
        init_funcs = self.py__getattribute__('__call__')
        return [sig.bind(self) for sig in init_funcs.get_signatures()]

    def __repr__(self):
        return "<%s of %s(%s)>" % (self.__class__.__name__, self.class_context,
                                   self.var_args)


class CompiledInstance(AbstractInstanceContext):
    def __init__(self, evaluator, parent_context, class_context, var_args):
        self._original_var_args = var_args
        super(CompiledInstance, self).__init__(evaluator, parent_context, class_context, var_args)

    @property
    def name(self):
        return compiled.CompiledContextName(self, self.class_context.name.string_name)

    def get_first_non_keyword_argument_contexts(self):
        key, lazy_context = next(self._original_var_args.unpack(), ('', None))
        if key is not None:
            return NO_CONTEXTS

        return lazy_context.infer()

    def is_stub(self):
        return False


class TreeInstance(AbstractInstanceContext):
    def __init__(self, evaluator, parent_context, class_context, var_args):
        # I don't think that dynamic append lookups should happen here. That
        # sounds more like something that should go to py__iter__.
        if class_context.py__name__() in ['list', 'set'] \
                and parent_context.get_root_context() == evaluator.builtins_module:
            # compare the module path with the builtin name.
            if settings.dynamic_array_additions:
                var_args = iterable.get_dynamic_array_instance(self, var_args)

        super(TreeInstance, self).__init__(evaluator, parent_context,
                                           class_context, var_args)
        self.tree_node = class_context.tree_node

    @property
    def name(self):
        return ContextName(self, self.class_context.name.tree_name)

    # This can recurse, if the initialization of the class includes a reference
    # to itself.
    @evaluator_method_cache(default=None)
    def _get_annotated_class_object(self):
        from jedi.evaluate.gradual.annotation import py__annotations__, \
            infer_type_vars_for_execution

        for func in self._get_annotation_init_functions():
            # Just take the first result, it should always be one, because we
            # control the typeshed code.
            bound = BoundMethod(self, func)
            execution = bound.get_function_execution(self.var_args)
            if not execution.matches_signature():
                # First check if the signature even matches, if not we don't
                # need to infer anything.
                continue

            all_annotations = py__annotations__(execution.tree_node)
            defined, = self.class_context.define_generics(
                infer_type_vars_for_execution(execution, all_annotations),
            )
            debug.dbg('Inferred instance context as %s', defined, color='BLUE')
            return defined
        return None

    def get_annotated_class_object(self):
        return self._get_annotated_class_object() or self.class_context

    def _get_annotation_init_functions(self):
        filter = next(self.class_context.get_filters())
        for init_name in filter.get('__init__'):
            for init in init_name.infer():
                if init.is_function():
                    for signature in init.get_signatures():
                        yield signature.context


class AnonymousInstance(TreeInstance):
    def __init__(self, evaluator, parent_context, class_context):
        super(AnonymousInstance, self).__init__(
            evaluator,
            parent_context,
            class_context,
            var_args=AnonymousInstanceArguments(self),
        )

    def get_annotated_class_object(self):
        return self.class_context  # This is the default.


class CompiledInstanceName(compiled.CompiledName):

    def __init__(self, evaluator, instance, klass, name):
        super(CompiledInstanceName, self).__init__(
            evaluator,
            klass.parent_context,
            name.string_name
        )
        self._instance = instance
        self._class_member_name = name

    @iterator_to_context_set
    def infer(self):
        for result_context in self._class_member_name.infer():
            if result_context.api_type == 'function':
                yield CompiledBoundMethod(result_context)
            else:
                yield result_context


class CompiledInstanceClassFilter(AbstractFilter):
    name_class = CompiledInstanceName

    def __init__(self, evaluator, instance, f):
        self._evaluator = evaluator
        self._instance = instance
        self._class_filter = f

    def get(self, name):
        return self._convert(self._class_filter.get(name))

    def values(self):
        return self._convert(self._class_filter.values())

    def _convert(self, names):
        klass = self._class_filter.compiled_object
        return [
            CompiledInstanceName(self._evaluator, self._instance, klass, n)
            for n in names
        ]


class BoundMethod(FunctionMixin, ContextWrapper):
    def __init__(self, instance, function):
        super(BoundMethod, self).__init__(function)
        self.instance = instance

    def is_bound_method(self):
        return True

    def py__class__(self):
        c, = contexts_from_qualified_names(self.evaluator, u'types', u'MethodType')
        return c

    def _get_arguments(self, arguments):
        if arguments is None:
            arguments = AnonymousInstanceArguments(self.instance)

        return InstanceArguments(self.instance, arguments)

    def get_function_execution(self, arguments=None):
        arguments = self._get_arguments(arguments)
        return super(BoundMethod, self).get_function_execution(arguments)

    def py__call__(self, arguments):
        if isinstance(self._wrapped_context, OverloadedFunctionContext):
            return self._wrapped_context.py__call__(self._get_arguments(arguments))

        function_execution = self.get_function_execution(arguments)
        return function_execution.infer()

    def get_signatures(self):
        return [sig.bind(self) for sig in super(BoundMethod, self).get_signatures()]

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._wrapped_context)


class CompiledBoundMethod(ContextWrapper):
    def is_bound_method(self):
        return True

    def get_signatures(self):
        return [sig.bind(self) for sig in self._wrapped_context.get_signatures()]


class SelfName(TreeNameDefinition):
    """
    This name calculates the parent_context lazily.
    """
    def __init__(self, instance, class_context, tree_name):
        self._instance = instance
        self.class_context = class_context
        self.tree_name = tree_name

    @property
    def parent_context(self):
        return self._instance.create_instance_context(self.class_context, self.tree_name)


class LazyInstanceClassName(object):
    def __init__(self, instance, class_context, class_member_name):
        self._instance = instance
        self.class_context = class_context
        self._class_member_name = class_member_name

    @iterator_to_context_set
    def infer(self):
        for result_context in self._class_member_name.infer():
            for c in apply_py__get__(result_context, self._instance, self.class_context):
                yield c

    def __getattr__(self, name):
        return getattr(self._class_member_name, name)

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._class_member_name)


class InstanceClassFilter(AbstractFilter):
    """
    This filter is special in that it uses the class filter and wraps the
    resulting names in LazyINstanceClassName. The idea is that the class name
    filtering can be very flexible and always be reflected in instances.
    """
    def __init__(self, evaluator, instance, class_filter):
        self._instance = instance
        self._class_filter = class_filter

    def get(self, name):
        return self._convert(self._class_filter.get(name, from_instance=True))

    def values(self):
        return self._convert(self._class_filter.values(from_instance=True))

    def _convert(self, names):
        return [LazyInstanceClassName(self._instance, self._class_filter.context, n) for n in names]

    def __repr__(self):
        return '<%s for %s>' % (self.__class__.__name__, self._class_filter.context)


class SelfAttributeFilter(ClassFilter):
    """
    This class basically filters all the use cases where `self.*` was assigned.
    """
    name_class = SelfName

    def __init__(self, evaluator, context, class_context, origin_scope):
        super(SelfAttributeFilter, self).__init__(
            evaluator=evaluator,
            context=context,
            node_context=class_context,
            origin_scope=origin_scope,
            is_instance=True,
        )
        self._class_context = class_context

    def _filter(self, names):
        names = self._filter_self_names(names)
        start, end = self._parser_scope.start_pos, self._parser_scope.end_pos
        return [n for n in names if start < n.start_pos < end]

    def _filter_self_names(self, names):
        for name in names:
            trailer = name.parent
            if trailer.type == 'trailer' \
                    and len(trailer.parent.children) == 2 \
                    and trailer.children[0] == '.':
                if name.is_definition() and self._access_possible(name, from_instance=True):
                    # TODO filter non-self assignments.
                    yield name

    def _convert_names(self, names):
        return [self.name_class(self.context, self._class_context, name) for name in names]

    def _check_flows(self, names):
        return names


class InstanceArguments(TreeArgumentsWrapper):
    def __init__(self, instance, arguments):
        super(InstanceArguments, self).__init__(arguments)
        self.instance = instance

    def unpack(self, func=None):
        yield None, LazyKnownContext(self.instance)
        for values in self._wrapped_arguments.unpack(func):
            yield values

    def get_executed_params_and_issues(self, execution_context):
        if isinstance(self._wrapped_arguments, AnonymousInstanceArguments):
            return self._wrapped_arguments.get_executed_params_and_issues(execution_context)

        return super(InstanceArguments, self).get_executed_params_and_issues(execution_context)
