"""
JSONP - JSON with Python-style multiline strings

This module provides json module compatible interfaces, but supports Python-style
triple-quoted multiline strings in .jsonp files.

The only difference between .jsonp and .json files is that .jsonp supports
triple-quoted strings for better readability of multiline text.

In memory, JSONP and JSON data are identical.
"""

import json
import re
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, TextIO, Union

jsonp_tag = "__JSONP__"
jsonp_tag_escaped = jsonp_tag + "_ESCAPED"


def _convert_to_triple_quoted(json_str: str) -> str:
    """
    Convert standard JSON string to JSONP format with triple-quoted strings.

    This function preserves JSON structure and indentation while converting
    \\n escape sequences in strings to actual newlines in triple-quoted format.

    Args:
        json_str: Standard JSON string from json.dumps()

    Returns:
        JSONP format string with triple-quoted multiline strings
    """
    result = json_str.replace(f'"{jsonp_tag}', '\n"""\n')
    result = result.replace(f"'{jsonp_tag}", "\n'''\n")
    result = result.replace(f'{jsonp_tag}"', '\n"""\n')
    result = result.replace(f"{jsonp_tag}'", "\n'''\n")
    result = result.replace(jsonp_tag_escaped, jsonp_tag)

    # Replace \\n with actual newlines only in triple-quoted strings
    result = re.sub(r'("""[\s\S]*?""")', lambda m: m.group(0).replace("\\n", "\n"), result)
    result = re.sub(r"('''[\s\S]*?''')", lambda m: m.group(0).replace("\\n", "\n"), result)

    # Fix escaped quotes in triple-quoted strings: \" -> "
    result = re.sub(r'("""[\s\S]*?""")', lambda m: m.group(0).replace('\\"', '"'), result)
    result = re.sub(r"('''[\s\S]*?''')", lambda m: m.group(0).replace('\\"', '"'), result)

    return result


def _convert_from_triple_quoted(jsonp_str: str) -> str:
    """
    Convert JSONP format string back to standard JSON string.

    Args:
        jsonp_str: JSONP format string with triple-quoted strings

    Returns:
        Standard JSON string from json.loads()
    """
    # Pattern to match triple-quoted strings
    # Handles both """...""" and '''...'''
    s = jsonp_str.replace('\n"""', '"""')
    s = s.replace("\n'''", "'''")
    pattern = r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')'

    def replace_with_standard_quotes(match):
        triple_quoted = match.group(1)
        quote_type = triple_quoted[:3]  # """ or '''
        content = triple_quoted[3:-3]  # Extract content without quotes

        # Don't strip newlines - preserve original formatting

        # Escape backslashes first (must be done before escaping newlines)
        content = content.replace("\\", "\\\\")

        # Convert newlines to \n escape sequences
        content = content.replace("\n", "\\n")

        # Escape quotes
        content = content.replace('"', '\\"')

        return f'"{content}"'

    return re.sub(pattern, replace_with_standard_quotes, s)


def dumps(obj: Any, **kwargs) -> str:
    """
    Serialize obj to a JSONP formatted str.

    Args:
        obj: Python object to serialize
        **kwargs: Arguments passed to json.dumps()

    Returns:
        JSONP format string with proper formatting
    """
    return json.dumps(obj, **kwargs)


def dump(obj: Any, fp: Union[TextIO, BinaryIO], **kwargs):
    """
    Serialize obj as a JSONP stream to fp (a .write()-supporting file-like object).

    Args:
        obj: Python object to serialize
        fp: File-like object to write to
        **kwargs: Arguments passed to json.dump()
    """
    # Set default indent to 2 for readability if not specified
    if "indent" not in kwargs:
        kwargs["indent"] = 2
    if "ensure_ascii" not in kwargs:
        kwargs["ensure_ascii"] = False

    # 遍历对象，找到所有字符串并检查是否包含换行转义符，如果包含则在字符串首尾加上三个双引号
    def check_and_convert_strings(obj):
        if isinstance(obj, str):
            if "\n" in obj.strip():
                # 标记为需要三引号字符串
                obj = obj.strip().replace(
                    jsonp_tag, jsonp_tag_escaped
                )  # Escape the tag if it appears in content
                return f"{jsonp_tag}{obj}{jsonp_tag}"
            else:
                return obj
        elif isinstance(obj, list):
            return [check_and_convert_strings(item) for item in obj]
        elif isinstance(obj, dict):
            return {key: check_and_convert_strings(value) for key, value in obj.items()}
        else:
            return obj

    obj = check_and_convert_strings(obj)

    # First get formatted JSON string with indentation
    json_str = json.dumps(obj, **kwargs)
    # Then convert \n escape sequences to actual newlines in triple-quoted strings
    jsonp_str = _convert_to_triple_quoted(json_str)

    # Check if it's a binary file
    if hasattr(fp, "mode") and "b" in fp.mode:
        # Binary mode - encode to bytes
        encoding = kwargs.get("encoding", "utf-8")
        fp.write(jsonp_str.encode(encoding))
    else:
        # Text mode - write directly
        fp.write(jsonp_str)


def loads(s: Union[str, bytes, bytearray], **kwargs) -> Any:
    """
    Deserialize s (a str, bytes or bytearray instance containing a JSONP document)
    to a Python object.

    Args:
        s: JSONP string to deserialize
        **kwargs: Arguments passed to json.loads()

    Returns:
        Python object
    """
    # Then parse as standard JSON
    return json.loads(s, **kwargs)


def load(fp: Union[TextIO, BinaryIO], **kwargs) -> Any:
    """
    Deserialize fp (a .read()-supporting text file or binary file containing
    a JSONP document) to a Python object.

    Args:
        fp: File-like object to read from
        **kwargs: Arguments passed to json.load()

    Returns:
        Python object
    """
    s = fp.read()
    # Convert bytes to string if necessary
    if isinstance(s, (bytes, bytearray)):
        # Auto-detect encoding
        encoding = kwargs.pop("encoding", None)
        if encoding is None:
            # Try UTF-8 first, then fallback to latin-1
            try:
                s = s.decode("utf-8")
            except UnicodeDecodeError:
                s = s.decode("latin-1")
        else:
            s = s.decode(encoding)

    # Convert from JSONP to standard JSON
    json_str = _convert_from_triple_quoted(s)

    return loads(json_str, **kwargs)


# Re-export all non-I/O json module functions and constants
JSONDecoder = json.JSONDecoder
JSONEncoder = json.JSONEncoder
JSONDecodeError = json.JSONDecodeError
