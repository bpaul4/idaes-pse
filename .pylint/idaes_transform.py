"""
Pylint transform plugin to make Pylint aware of custom `ProcessBlock` classes
dynamically created through the `declare_process_block_class()` decorator.

See #1159 for more information.
"""

from dataclasses import dataclass
import sys
import functools
import logging
import time
import typing

import astroid
import pylint
from astroid.builder import extract_node, parse


_logger = logging.getLogger("pylint.ideas_plugin")


# TODO figure out a better way to integrate this with pylint logging and/or verbosity settings
_display = _notify = lambda *a, **kw: None


def _suppress_inference_errors(max_inferred=500) -> None:
    """
    Increasing this number to suppress inference errors causing false-positives in e.g. pandas.read_csv().
    The value 500 is a sufficiently high number but otherwise arbitrary.
    See https://github.com/PyCQA/pylint/issues/4577 for more information.
    """
    astroid.context.InferenceContext.max_inferred = int(max_inferred)


_suppress_inference_errors()


@dataclass
class VersionCompat:
    distr_name: str
    expected: str
    actual: str

    @property
    def cmd_to_install(self) -> str:
        return f"pip install {self.distr_name}=={self.expected}"


def _check_version_compatibility() -> None:
    to_check = [
        VersionCompat(
            distr_name="pylint",
            expected="3.0.3",
            actual=pylint.__version__,
        ),
        VersionCompat(
            distr_name="astroid",
            expected="3.0.3",
            actual=astroid.__version__,
        ),
    ]

    for v in to_check:
        if v.actual != v.expected:
            msg = (
                f"WARNING: this plugin's reference version for {v.distr_name} is {v.expected}, "
                f"but the currently installed version for is {v.actual}. "
                "This is not necessarily a problem; "
                f"however, in case of issues, try installing the reference version using {v.cmd_to_install}"
            )
            print(msg, file=sys.stderr)


def has_declare_block_class_decorator(
    cls_node, decorator_name="declare_process_block_class"
):
    if "idaes" not in cls_node.root().name:
        return False
    decorators = cls_node.decorators
    if not decorators:
        return False
    for dec_subnode in decorators.nodes:
        if hasattr(dec_subnode, "func"):
            # this is true for decorators with arguments
            return dec_subnode.func.as_string() == decorator_name
    return False


# this will be called N times if running with N processes
# returning the cached result on subsequent calls
@functools.lru_cache(maxsize=1)
def get_base_class_node():
    _notify("Getting base class node")
    pb_def = """
        import idaes.core.base.process_block.ProcessBlock
        class ProcessBlock(idaes.core.base.process_block.ProcessBlock):
            # creating a stub for the __getitem__() method returning the uninferable object
            # causes pylint to stop further checks on objects returned when calling [] on a derived class
            # see e.g. https://github.com/PyCQA/astroid/blob/main/astroid/brain/brain_numpy_ndarray.py
            # for another example of how this can be extended to create "uninferability stubs" for other methods
            def __getitem__(self, *args): return uninferable
    """
    import_node = astroid.extract_node(pb_def)
    cls_node = next(import_node.infer())
    return cls_node


def add_attribute_nodes(node: astroid.ClassDef, attr_names):
    for attr_name in attr_names:
        rhs_node = astroid.Unknown(
            lineno=node.lineno,
            parent=node,
        )
        node.locals[attr_name] = [rhs_node]
        node.instance_attrs[attr_name] = [rhs_node]


def create_declared_class_node(decorated_cls_node: astroid.ClassDef):
    decorators = decorated_cls_node.decorators
    call = decorators.nodes[0]
    name_arg_node = call.args[0]
    decl_class_name = name_arg_node.value

    base_class_node = get_base_class_node()
    decl_class_node = astroid.ClassDef(
        decl_class_name,
        # TODO the real doc should be available as the "doc" kwarg of the decorator
        # but it's not clear if we're going to need it anyway
        col_offset=decorated_cls_node.col_offset,
        parent=decorated_cls_node.parent,
        lineno=decorated_cls_node.lineno,
        end_lineno=decorated_cls_node.end_lineno,
        end_col_offset=decorated_cls_node.end_col_offset,
    )
    decl_class_node.bases.extend(
        [
            base_class_node,
            decorated_cls_node,
        ]
    )
    # doesn't seem to be needed at the moment
    # add_attribute_nodes(decl_class_node, ['_orig_name', '_orig_module'])
    return decl_class_node


def is_idaes_module(mod_node: astroid.Module):
    mod_name = mod_node.name
    return "idaes" in mod_name


def register_process_block_class(decorated_cls_node: astroid.ClassDef):
    module_node = decorated_cls_node.parent
    _display(module_node.name)
    decl_class_node = create_declared_class_node(decorated_cls_node)


def iter_process_block_data_classes(mod_node: astroid.Module):
    for node in mod_node.body:
        if isinstance(node, astroid.ClassDef):
            if has_declare_block_class_decorator(node):
                yield node


def register_process_block_classes(mod_node: astroid.Module):
    _display(mod_node.name)
    for decorated_cls_node in iter_process_block_data_classes(mod_node):
        decl_cls_node = create_declared_class_node(decorated_cls_node)
        _display(f"{decorated_cls_node.name} -> {decl_cls_node.name}")


def is_config_block_class(node: astroid.ClassDef):
    # NOTE might be necessary to use node.qname() instead, but that's more prone to breaking
    # if the internal package structure is changed
    return "ConfigDict" in node.name


def disable_attr_checks_on_slots(node: astroid.ClassDef):
    # ConfigDict/ConfigBlock defines __slots__, which trigger a pylint error
    # whenever a value is set on a non-slot attribute
    # in reality, ConfigDict/ConfigBlock objects do support setting attributes at runtime
    # via their __setattr__() method
    # a quick fix for this false positive is to remove __slots__ from the ClassDef scope,
    # which has the same effect as though __slots__ were not defined in the first place
    # Although this is arguably mostly a shortcut to get rid of the pylint false positive,
    # it is nonetheless consistent with the general behavior of this class,
    # considering how the "loose" behavior of its __setattr__() method
    # overrides the "strict" semantics of having __slots__ defined
    # NOTE to be extra defensive, we should probably make sure that there are
    # no __slots__ defined throughout the complete class hierarchy as well
    try:
        del node.locals["__slots__"]
    except KeyError as e:
        pass


def has_conditional_instantiation(node: astroid.ClassDef, context=None):
    if "pyomo" not in node.qname():
        return
    try:
        # check if the class defines a __new__()
        dunder_new_node: astroid.FunctionDef = node.local_attr("__new__")[0]
    except astroid.AttributeInferenceError:
        return False
    else:
        # _display(node)
        # find all return statements; if there's more than one, assume that instances are created conditionally,
        # and therefore the type of the instantiated object cannot be known with static analysis
        # to be more accurate, we should check for If nodes as well as maybe the presence of other __new__() calls
        return_statements = list(
            dunder_new_node.nodes_of_class(astroid.node_classes.Return)
        )
        return len(return_statements) > 1


def make_node_create_uninferable_instance(node: astroid.ClassDef, context=None):
    def _instantiate_uninferable(*args, **kwargs):
        return astroid.Uninferable()

    node.instantiate_class = _instantiate_uninferable
    return node


astroid.MANAGER.register_transform(
    # NOTE both these options were tried to see if there was a difference in performance,
    # but it doesn't seem to be the case at this point
    # astroid.ClassDef, register_process_block_class, has_declare_block_class_decorator
    astroid.Module,
    register_process_block_classes,
    is_idaes_module,
)

astroid.MANAGER.register_transform(
    astroid.ClassDef, disable_attr_checks_on_slots, is_config_block_class
)


astroid.MANAGER.register_transform(
    astroid.ClassDef,
    make_node_create_uninferable_instance,
    predicate=has_conditional_instantiation,
)


def register(linter):
    "This function needs to be defined for the plugin to be picked up by pylint"
    _check_version_compatibility()
