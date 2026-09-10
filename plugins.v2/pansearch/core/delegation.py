"""组合式职责对象的状态委托。"""


class OwnerDelegator:
    """将职责对象未定义的属性读写委托给所属对象。"""

    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __setattr__(self, name, value):
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        setattr(self._owner, name, value)


def get_component(owner, component_type, cache_name: str):
    """按类型创建并缓存职责组件。"""
    components = owner.__dict__.setdefault(cache_name, {})
    component = components.get(component_type)
    if component is None:
        component = component_type(owner)
        components[component_type] = component
    return component


def resolve_component(owner, component_types, name: str, cache_name: str):
    """从职责组件中解析宿主未直接定义的属性。"""
    for component_type in component_types:
        if hasattr(component_type, name):
            component = get_component(owner, component_type, cache_name)
            return getattr(component, name)
    raise AttributeError(
        f"{owner.__class__.__name__!s} object has no attribute {name!r}"
    )
