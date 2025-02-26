import importlib
import importlib.util
import inspect
import json
import logging
import sys
from enum import Enum
from typing import Any, Literal, Type, Union

from pydantic import BaseModel
from pydantic.fields import FieldInfo

logging.basicConfig(level=logging.INFO)

TOOL_DIRS = ["istio", "k8s", "prometheus", "docs"]


def create_dummy_args(model_fields: dict[str, FieldInfo]):
    """
    Generate dummy arguments for a Pydantic model based on its model_fields.

    Args:
        model_fields: The model_fields dictionary from a Pydantic v2 model

    Returns:
        dict: A dictionary of field names to appropriate dummy values
    """
    dummy_args = {}

    for field_name, field_info in model_fields.items():
        # Get the annotation (type) of the field
        field_type = field_info.annotation

        # Check if field has a default value (not PydanticUndefined)
        # PydanticUndefined typically has a class name containing "Undefined"
        default_undefined = (
            field_info.default is ...
            or field_info.default is None
            or (hasattr(field_info.default, "__class__") and "Undefined" in field_info.default.__class__.__name__)
        )

        if not default_undefined:
            dummy_args[field_name] = field_info.default
            continue

        # Handle Optional types (Union with None)
        is_optional = False
        if hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
            if type(None) in field_type.__args__:
                is_optional = True
                # Get the actual type (excluding None)
                non_none_types = [arg for arg in field_type.__args__ if arg is not type(None)]
                if non_none_types:
                    field_type = non_none_types[0]

        # Create appropriate dummy values based on type
        if field_type is str or (hasattr(field_type, "__name__") and field_type.__name__ == "str"):
            dummy_args[field_name] = ""
        elif field_type is int or (hasattr(field_type, "__name__") and field_type.__name__ == "int"):
            dummy_args[field_name] = 42
        elif field_type is float or (hasattr(field_type, "__name__") and field_type.__name__ == "float"):
            dummy_args[field_name] = 3.14
        elif field_type is bool or (hasattr(field_type, "__name__") and field_type.__name__ == "bool"):
            dummy_args[field_name] = True
        elif hasattr(field_type, "__origin__") and field_type.__origin__ is list:
            # Handle list types, can create empty list or dummy items
            dummy_args[field_name] = []
        elif hasattr(field_type, "__origin__") and field_type.__origin__ is dict:
            # Handle dict types
            dummy_args[field_name] = {}
        elif hasattr(field_type, "__origin__") and field_type.__origin__ is Literal:
            # For Literal types, use the first allowed value
            dummy_args[field_name] = field_type.__args__[0]
        elif field_type is Any:
            # For Any type
            dummy_args[field_name] = ""
        elif is_optional:
            # For optional fields, use field name (as you requested)
            dummy_args[field_name] = field_name
        elif inspect.isclass(field_type) and issubclass(field_type, Enum):
            # Handle Enum types by selecting first value
            dummy_args[field_name] = next(iter(field_type))
        elif inspect.isclass(field_type) and issubclass(field_type, BaseModel):
            # For nested Pydantic models, recursively create dummy values
            if hasattr(field_type, "model_fields"):
                nested_args = create_dummy_args(field_type.model_fields)
                dummy_args[field_name] = field_type(**nested_args)
            else:
                dummy_args[field_name] = None
        else:
            # Default case for unknown types
            dummy_args[field_name] = field_name

    return dummy_args


def get_tool_json(obj: Type[any], config: dict) -> dict:
    description = ""

    if "_description" in obj.__dict__:
        description = obj.__dict__.get("_description")
    if not description and "component_description" in obj.__dict__:
        description = obj.__dict__.get("component_description")
    if not description and hasattr(obj, "__doc__"):
        description = obj.__doc__

    provider = obj.__dict__.get("component_provider_override", "")
    if not provider:
        # The tool clases don't have component_provider_override attribute
        # however, the class __module__ attribute contains the first part of the provider (minus the module name, e.g. kagent.tools.istio._istioctl)
        # and the obj.__name__ is the actual tool name.
        provider = f"{obj.__module__.rsplit('.', 1)[0]}.{obj.__name__}"

    return {
        "provider": provider,
        "description": description,
        "component_type": "tool",
        "version": 1,
        "label": obj.__name__,
        "config": json.loads(config) if config else {},
    }


def get_config_instance(config_obj):
    if issubclass(config_obj, BaseModel):
        if hasattr(config_obj, "model_fields"):
            dummy_args = create_dummy_args(config_obj.model_fields)
            try:
                config_instance = config_obj(**dummy_args)
                return config_instance
            except Exception as e:
                logging.error(f"Error instantiating config: {e}")
        return None


def get_tool_configs(module_path: str) -> list:
    module = importlib.import_module(f"kagent.tools.{module_path}")

    tool_components = []
    module_objects = dict(inspect.getmembers(module))

    for name in module_objects.get("__all__", []):
        obj = getattr(module, name)
        if not inspect.isclass(obj) or not hasattr(obj, "__mro__"):
            continue

        # Skip if class isn't a BaseTool subclass
        if not any(base.__name__ == "BaseTool" for base in obj.__mro__):
            continue

        config_instance = None

        # We'll be relying on a naming convention for the config class
        # 1. If the tool has a config class, it's named as the tool + "Config" (e.g. MyTool -> MyToolConfig)
        # 2. If there's a single Config class for the set of same tools (e.g. "global" config), it's named as "Config" (e.g. MyTool -> Config)
        # 3. If there's no config class, we'll try to instantiate the tool without a config

        # Check if there's a corresponding config class that's named as the tool + "Config"
        config_name = f"{name}Config"
        if config_name in module_objects:
            config_obj = module_objects[config_name]
            config_instance = get_config_instance(config_obj)
        else:
            sig = inspect.signature(obj)
            if len(sig.parameters) == 1 and "config" in sig.parameters:
                config_obj = module_objects["Config"]
                if not config_obj:
                    raise Exception(f"Expected default 'Config' class for {name}")
                config_instance = get_config_instance(config_obj)

        tool_json = get_tool_json(
            obj, config_instance.model_dump_json(exclude={"description"}) if config_instance else None
        )
        tool_components.append(tool_json)

    return tool_components


def main():
    all_config = []

    for dir_name in TOOL_DIRS:
        try:
            tools = get_tool_configs(dir_name)
            all_config.extend(tools)
        except Exception as e:
            logging.error(f"Error processing directory {dir_name}: {e}")

    # write to a file
    output_file = "tools.json"
    if len(sys.argv) > 1:
        output_file = sys.argv[1]

    with open(output_file, "w") as f:
        json.dump(all_config, f, indent=2, default=str)

    logging.info(
        f"Tool configurations written to {output_file}. Copy the file to $HOME/.autogenstudio/configs/tools.json to import it to the backend."
    )


if __name__ == "__main__":
    main()
