# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Small jsonargparse adapters used by the public configuration APIs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from types import UnionType
from typing import TYPE_CHECKING, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from jsonargparse import ArgumentParser

if TYPE_CHECKING:
    from pathlib import Path

T = TypeVar("T")


def _enum_wire_values(value: object, annotation: object) -> object:  # ruff: ignore[too-many-return-statements]
    """Adapt existing value-based enum serialization to jsonargparse names.

    Returns:
        A value compatible with jsonargparse's enum handling.
    """
    if value is None:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {UnionType, Union}:
        for option in args:
            if option is type(None):
                continue
            converted = _enum_wire_values(value, option)
            if converted is not value:
                return converted
        return value
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if isinstance(value, annotation):
            return value.name
        for member in annotation:
            if member.value == value:
                return member.name
        return value
    if origin in {list, tuple, set} and isinstance(value, (list, tuple)):
        item_type = args[0] if args else object
        return [_enum_wire_values(item, item_type) for item in value]
    if origin is dict and isinstance(value, Mapping):
        value_type = args[1] if len(args) > 1 else object
        return {key: _enum_wire_values(item, value_type) for key, item in value.items()}
    if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, Mapping):
        hints = get_type_hints(annotation)
        return {
            field.name: _enum_wire_values(item, hints.get(field.name, field.type))
            for field in fields(annotation)
            if field.name in value
            for item in [value[field.name]]
        }
    return value


def _parser_for_class(target: type[T], root: str) -> ArgumentParser:
    parser = ArgumentParser(exit_on_error=False)
    parser.add_class_arguments(target, root)
    return parser


def parse_class_config(
    target: type[T],
    data: Mapping[str, object],
    *,
    defaults: bool = False,
) -> T:
    """Parse and instantiate flat arguments for a known class.

    Returns:
        An instance of *target*.
    """
    parser = _parser_for_class(target, "object")
    values = _enum_wire_values(dict(data), target)
    namespace = parser.parse_object({"object": values}, defaults=defaults)
    return cast("T", parser.instantiate(namespace).object)


def parse_class_config_file(
    target: type[T],
    path: str | Path,
    *,
    defaults: bool = False,
) -> T:
    """Parse and instantiate a known class from a YAML or JSON file.

    Returns:
        An instance of *target*.
    """
    parser = _parser_for_class(target, "object")
    namespace = parser.parse_path(path, defaults=defaults)
    return cast("T", parser.instantiate(namespace).object)


def instantiate_known_config(
    target: type[T],
    source: Mapping[str, object] | str | Path,
    *,
    defaults: bool = False,
) -> T:
    """Parse and instantiate a known class from a mapping or file.

    Returns:
        An instance of *target*.
    """
    if isinstance(source, Mapping):
        return parse_class_config(target, source, defaults=defaults)
    return parse_class_config_file(target, source, defaults=defaults)


def parse_component(
    base: type[T],
    spec: Mapping[str, object],
    *,
    defaults: bool = False,
) -> T:
    """Parse and instantiate a class recipe constrained to *base*.

    Returns:
        An instance compatible with *base*.
    """
    parser = ArgumentParser(exit_on_error=False)
    parser.add_subclass_arguments(base, "component", required=True)
    namespace = parser.parse_object({"component": dict(spec)}, defaults=defaults)
    return cast("T", parser.instantiate(namespace).component)


__all__ = [
    "instantiate_known_config",
    "parse_class_config",
    "parse_class_config_file",
    "parse_component",
]
