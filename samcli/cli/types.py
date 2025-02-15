"""
Implementation of custom click parameter types
"""

import json
import logging
import re
from json import JSONDecodeError
from typing import Dict, List, Optional, Union

import click

from samcli.lib.package.ecr_utils import is_ecr_url

PARAM_AND_METADATA_KEY_REGEX = """([A-Za-z0-9\\"\']+)"""

LOG = logging.getLogger(__name__)


def _generate_match_regex(match_pattern, delim=None):
    """
    Creates a regex string based on a match pattern (also a regex) that is to be
    run on a string (which may contain escaped quotes) that is separated by delimiters.

    Parameters
    ----------
    match_pattern: (str) regex pattern to match
    delim: (str) delimiter that is respected when identifying matching groups with generated regex.

    Returns
    -------
    str: regex expression

    """
    result = f"""(\\"(?:\\\\{match_pattern}|[^\\"\\\\]+)*\\"|""" + f"""\'(?:\\\\{match_pattern}|[^\'\\\\]+)*\'"""

    if delim is not None:
        # Non capturing groups reduces duplicates in groups, but does not reduce matches.
        return result + f"""|(?:\\\\{match_pattern}|[^{delim}\\"\\\\]+)+)"""
    else:
        return result + ")"


def _unquote_wrapped_quotes(value):
    r"""
    Removes wrapping single or double quotes and unescapes '\ ', '\"' and '\''.

    Parameters
    ----------
    value : str
        Input to unquote

    Returns
    -------
    Unquoted string
    """

    if value and value[0] == value[-1] and value[0] in ('"', "'") and value[-1] in ('"', "'"):
        value = value[1:-1]

    return value.replace("\\ ", " ").replace('\\"', '"').replace("\\'", "'")


class CfnParameterOverridesType(click.ParamType):
    """
    Custom Click options type to accept values for CloudFormation template parameters. You can pass values for
    parameters as "ParameterKey=KeyPairName,ParameterValue=MyKey ParameterKey=InstanceType,ParameterValue=t1.micro"
    """

    __EXAMPLE_1 = "ParameterKey=KeyPairName,ParameterValue=MyKey ParameterKey=InstanceType,ParameterValue=t1.micro"
    __EXAMPLE_2 = "KeyPairName=MyKey InstanceType=t1.micro"

    # Regex that parses CloudFormation parameter key-value pairs:
    # https://regex101.com/r/xqfSjW/2
    # https://regex101.com/r/xqfSjW/5

    # If Both ParameterKey pattern and KeyPairName=MyKey should not be present
    # while adding parameter overrides, if they are, it
    # can result in unpredicatable behavior.
    # Use this regex when you have space as delimiter Ex: "KeyName1=string KeyName2=string"
    VALUE_REGEX_SPACE_DELIM = _generate_match_regex(match_pattern=".", delim=" ")
    _pattern_1 = r"(?:ParameterKey={key},ParameterValue={value})".format(
        key=PARAM_AND_METADATA_KEY_REGEX, value=VALUE_REGEX_SPACE_DELIM
    )
    _pattern_2 = r"(?:(?: ){key}={value})".format(key=PARAM_AND_METADATA_KEY_REGEX, value=VALUE_REGEX_SPACE_DELIM)

    ordered_pattern_match = [_pattern_1, _pattern_2]

    name = "string,list"

    def convert(self, value, param, ctx):
        result = {}

        # Empty tuple
        if value == ("",):
            return result

        value = (value,) if isinstance(value, str) else value
        for val in value:
            # Add empty string to start of the string to help match `_pattern2`
            normalized_val = " " + val.strip()

            try:
                # NOTE(TheSriram): find the first regex that matched.
                # pylint is concerned that we are checking at the same `val` within the loop,
                # but that is the point, so disabling it.
                pattern = next(
                    i
                    for i in filter(
                        lambda item: re.findall(item, normalized_val), self.ordered_pattern_match
                    )  # pylint: disable=cell-var-from-loop
                )
            except StopIteration:
                return self.fail(
                    "{} is not in valid format. It must look something like '{}' or '{}'".format(
                        val, self.__EXAMPLE_1, self.__EXAMPLE_2
                    ),
                    param,
                    ctx,
                )

            groups = re.findall(pattern, normalized_val)

            # 'groups' variable is a list of tuples ex: [(key1, value1), (key2, value2)]
            for key, param_value in groups:
                result[_unquote_wrapped_quotes(key)] = _unquote_wrapped_quotes(param_value)

        return result


class CfnMetadataType(click.ParamType):
    """
    Custom Click options type to accept values for metadata parameters.
    metadata parameters can be of the type KeyName1=string,KeyName2=string or {"string":"string"}
    """

    _EXAMPLE = 'KeyName1=string,KeyName2=string or {"string":"string"}'
    # Use this regex when you have comma as delimiter Ex: "KeyName1=string,KeyName2=string"
    VALUE_REGEX_COMMA_DELIM = _generate_match_regex(match_pattern=".", delim=",")

    _pattern = r"(?:{key}={value})".format(key=PARAM_AND_METADATA_KEY_REGEX, value=VALUE_REGEX_COMMA_DELIM)

    name = "string"

    def convert(self, value, param, ctx):
        result = {}
        fail = False
        if not value:
            return result
        try:
            # Look to load the value into json if we can.
            result = json.loads(value)
            for val in result.values():
                if isinstance(val, (dict, list)):
                    # Need a non nested dictionary or a dictionary with non list values,
                    # If either is found, fail the conversion.
                    fail = True
        except JSONDecodeError:
            # if looking for a json format failed, look at if the specified value follows
            # KeyName1=string,KeyName2=string format
            groups = re.findall(self._pattern, value)

            if not groups:
                fail = True
            for group in groups:
                key, v = group
                # assign to result['KeyName1'] = string and so on.
                result[key] = v

        if fail:
            return self.fail(
                "{} is not in valid format. It must look something like '{}'".format(value, self._EXAMPLE), param, ctx
            )

        return result


class CfnTags(click.ParamType):
    """
    Custom Click options type to accept values for tag parameters.
    tag parameters can be of the type KeyName1=string KeyName2=string

    If multiple_values_per_key is set to True, the returned dictionary will map
    each key to a list of corresponding values.

    E.g. Input: KeyName1=Value1 KeyName1=Value2 Output: {KeyName1 : [Value1, Value2]}
    """

    def __init__(self, multiple_values_per_key=False):
        self.multiple_values_per_key = multiple_values_per_key

    _EXAMPLE = "KeyName1=string KeyName2=string"
    # Tags have additional constraints and they allow "+ - = . _ : / @" apart from alpha-numerics.
    TAG_REGEX = '[A-Za-z0-9\\"_:\\.\\/\\+-\\@=]'

    _pattern = r"{tag}={tag}".format(tag=_generate_match_regex(match_pattern=TAG_REGEX, delim=" "))
    _quoted_pattern = _generate_match_regex(match_pattern=TAG_REGEX)

    name = "string,list"

    def convert(self, value, param, ctx):
        result = {}
        fail = False
        # Empty tuple
        if value == ("",):
            return result

        # if value comes in via samconfig file and is a list, it is parsed to string.
        if isinstance(value, list):
            if not value:
                return result
            value = tuple(value)
        # if value comes in a via configuration file, it will be a string. So we should still convert it.
        value = (value,) if not isinstance(value, tuple) else value

        for val in value:
            # Using standard parser first.
            # We should implement other type parser like JSON and Key=key,Value=val type format.
            parsed, tags = self._standard_key_value_parser(val)
            if not parsed:
                parsed, tags = self._space_separated_key_value_parser(val)
            if parsed:
                for k in tags:
                    self._add_value(result, _unquote_wrapped_quotes(k), _unquote_wrapped_quotes(tags[k]))
            else:
                fail = not self._parse_key_value_pair(result, val)

            if fail:
                return self.fail(
                    "{} is not in valid format. It must look something like '{}'".format(value, self._EXAMPLE),
                    param,
                    ctx,
                )

        return result

    def _parse_key_value_pair(self, result: dict, key_value_string: str):
        """
        This method processes a string in the format "'key1'='value1','key2'='value2'",
        where spaces may exist within keys or values.

        To optimize performance, the parsing is divided into two stages:

        Stage 1: Optimized Parsing
        1. Identify quoted strings containing spaces within values.
        2. Temporarily replace spaces in these strings with a placeholder (e.g., "_").
        3. Use a fast, standard parser to extract key-value pairs, as no spaces are expected.
        4. Restore original spaces in the extracted key-value pairs.

        Stage 2: Fallback Parsing
        If Stage 1 fails to parse the string correctly,run against a comprehensive regex pattern
        {tag}={tag}) to parse the entire string.

        Parameters
        ----------
        result: result dict
        key_value_string: string to parse

        Returns
        -------
        boolean - parse result
        """
        parse_result = True

        # Unquote an entire string
        modified_val = _unquote_wrapped_quotes(key_value_string)

        # Looking for a quote strings that contain spaces and proceed to replace them
        quoted_strings_with_spaces = re.findall(self._quoted_pattern, modified_val)
        quoted_strings_with_spaces_objects = [
            TextWithSpaces(str_with_spaces) for str_with_spaces in quoted_strings_with_spaces
        ]
        for s, replacement in zip(quoted_strings_with_spaces, quoted_strings_with_spaces_objects):
            modified_val = modified_val.replace(s, replacement.replace_spaces())

        # Use default parser to parse key=value
        tags = self._multiple_space_separated_key_value_parser(modified_val)
        if tags is not None:
            for key, value in tags.items():
                new_value = value
                text_objects = [obj for obj in quoted_strings_with_spaces_objects if obj.modified_text == value]
                if len(text_objects) > 0:
                    new_value = text_objects[0].restore_spaces()
                self._add_value(result, _unquote_wrapped_quotes(key), _unquote_wrapped_quotes(new_value))
        else:
            # Otherwise, fall back to the original mechanism.
            groups = re.findall(self._pattern, key_value_string)

            if not groups:
                parse_result = False
            for group in groups:
                key, v = group
                self._add_value(result, _unquote_wrapped_quotes(key), _unquote_wrapped_quotes(v))

        return parse_result

    def _add_value(self, result: dict, key: str, new_value: str):
        """
        Add a given value to a given key in the result map.
        """
        if self.multiple_values_per_key:
            if not result.get(key):
                result[key] = []
            result[key].append(new_value)
            return
        result[key] = new_value

    @staticmethod
    def _standard_key_value_parser(tag_value):
        """
        Method to parse simple `Key=Value` type tags without using regex. This is similar to how aws-cli does this.
        https://github.com/aws/aws-cli/blob/eff79a263347e8e83c8a2cc07265ab366315a992/awscli/customizations/cloudformation/deploy.py#L361
        Parameters
        ----------
        tag_value

        Returns
        -------

        """
        equals_count = tag_value.count("=")
        if equals_count != 1:
            return False, None

        splits = tag_value.split("=")
        return True, {splits[0]: splits[1]}

    @staticmethod
    def _space_separated_key_value_parser(tag_value):
        """
        Method to parse space separated `Key1=Value1 Key2=Value2` type tags without using regex.
        Parameters
        ----------
        tag_value
        """
        tags_dict = {}
        for value in tag_value.split(" "):
            parsed, parsed_tag = CfnTags._standard_key_value_parser(value)
            if not parsed:
                return False, None
            tags_dict = {**tags_dict, **parsed_tag}
        return True, tags_dict

    @staticmethod
    def _multiple_space_separated_key_value_parser(tag_value):
        """
        Method to parse space separated `Key1=Value1 Key2=Value2` type tags without using regex.
        Parameters
        ----------
        tag_value
        """
        tags_dict = {}
        for value in tag_value.split():
            parsed, parsed_tag = CfnTags._standard_key_value_parser(value)
            if not parsed:
                return None
            tags_dict.update(parsed_tag)
        return tags_dict


class SigningProfilesOptionType(click.ParamType):
    """
    Custom parameter type to parse Signing Profile options
    Example options could be;
    - MyFunctionOrLayerToBeSigned=MySigningProfile
    - MyFunctionOrLayerToBeSigned=MySigningProfile:MyProfileOwner

    See convert function docs for details
    """

    pattern = r"(?:(?: )([A-Za-z0-9\"]+)=(\"(?:\\.|[^\"\\]+)*\"|(?:\\.|[^ \"\\]+)+))"

    name = "string"

    def convert(self, value, param, ctx):
        """
        Converts given Signing Profile options to a dictionary where Function or Layer name would be key,
        and signing profile details would be the value.

        Since this method is also been used when we are reading the config details from samconfig.toml,
        If value is already a dictionary, don't need to do anything, just return it.
        If value is an array, then each value will correspond to a function or layer config, and here it is parsed
        and converted into a dictionary
        """
        result = {}

        # Empty tuple
        if value == ("",):
            return result

        value = (value,) if isinstance(value, str) else value
        for val in value:
            # Add empty string to start of the string to help match `_pattern2`
            normalized_val = " " + val.strip()

            signing_profiles = re.findall(self.pattern, normalized_val)

            # if no signing profiles found by regex, then fail
            if not signing_profiles:
                return self.fail(
                    f"{value} is not a valid code sign config, it should look like this 'MyFunction=SigningProfile'",
                    param,
                    ctx,
                )

            for function_name, param_value in signing_profiles:
                (signer_profile_name, signer_profile_owner) = self._split_signer_profile_name_owner(
                    _unquote_wrapped_quotes(param_value)
                )

                # code signing requires profile name, if it is not present then fail
                if not signer_profile_name:
                    return self.fail(
                        "Signer profile option has invalid format, it should look like this "
                        "MyFunction=MySigningProfile or MyFunction=MySigningProfile:MySigningProfileOwner",
                        param,
                        ctx,
                    )

                result[_unquote_wrapped_quotes(function_name)] = {
                    "profile_name": signer_profile_name,
                    "profile_owner": signer_profile_owner,
                }

        return result

    @staticmethod
    def _split_signer_profile_name_owner(signing_profile):
        equals_count = signing_profile.count(":")

        if equals_count > 1:
            return None, None
        if equals_count == 1:
            return signing_profile.split(":")
        return signing_profile, ""


class ImageRepositoryType(click.ParamType):
    """
    Custom Parameter Type for Image Repository option.
    """

    class Transformer:
        """
        Class takes in a converter (native click type) and a transformer.
        transformer is a function callback where additional transformation happens before
        conversion to a native click type.
        """

        def __init__(self, converter, transformation):
            """

            :param converter: native click Type
            :param transformation: callback function for transformation prior to conversion.
            """
            self.converter = converter
            self.transformer = transformation

        def transform(self, *args, **kwargs):
            return self.transformer(self.converter.convert(*args, **kwargs))

    # Transformation callback function checks if the received option value is a valid ECR url.
    transformer = Transformer(converter=click.STRING, transformation=is_ecr_url)
    name = "string"

    def convert(self, value, param, ctx):
        """
        Attempt a conversion given the stipulations of allowed transformations.
        """
        result = self.transformer.transform(value, param, ctx)
        if not result:
            raise click.BadParameter(f"Invalid Image Repository ECR URI: {value}")
        return value


class ImageRepositoriesType(click.ParamType):
    """
    Custom Parameter Type for Multi valued Image Repositories option.
    """

    name = "list"
    KEY_VALUE_PAIR_LENGTH = 2

    def convert(self, value, param, ctx):
        key_value_pair = value.split("=")
        if len(key_value_pair) != self.KEY_VALUE_PAIR_LENGTH:
            raise click.BadParameter(
                f"{param.opts[0]} is not a valid format, it needs to be of the form function_logical_id=ECR_URI"
            )
        key = key_value_pair[0]
        _value = key_value_pair[1]
        if not is_ecr_url(_value):
            raise click.BadParameter(f"{param.opts[0]} needs to have valid ECR URI as value")
        return {key: _value}


class RemoteInvokeBotoApiParameterType(click.ParamType):
    """
    Custom Parameter Type for Multi valued Boto API parameter option of remote invoke command.
    """

    name = "list"
    MIN_KEY_VALUE_PAIR_LENGTH = 2

    def convert(self, value, param, ctx):
        """Converts the user provided parameter value with the format "parameter=value" to dict
            {"parameter": "value"}

        Parameters
        ------------
        value: User provided value for the click option
        param: click parameter
        ctx: Context
        """
        # Split by first "=" as some values could have multiple "=" For e.g. base-64 encoded ClientContext for Lambda
        key_value_pair = value.split("=", 1)
        if len(key_value_pair) < self.MIN_KEY_VALUE_PAIR_LENGTH:
            raise click.BadParameter(
                f"{param.opts[0]} is not a valid format, it needs to be of the form parameter_key=parameter_value"
            )
        key = key_value_pair[0]
        _value = key_value_pair[1]
        LOG.debug("Converting provided %s option value to dict", param.opts[0])
        return {key: _value}


class DockerAdditionalHostType(click.ParamType):
    """
    Custom Parameter Type for managing Docker container's host file for local commands
    """

    name = "list"
    MIN_KEY_VALUE_PAIR_LENGTH = 2

    def convert(self, value, param, ctx):
        """Converts the user provided parameters value with the format "host:IP" to dict
            {"host": "IP"}

        Parameters
        ------------
        value: User provided value for the click option
        param: click parameter
        ctx: Context
        """
        host_ip_pair = value.split(":", maxsplit=1)
        if len(host_ip_pair) < self.MIN_KEY_VALUE_PAIR_LENGTH:
            raise click.BadParameter(f"{param.opts[0]} is not a valid format, it needs to be of the form hostname:IP")
        host = host_ip_pair[0]
        ip = host_ip_pair[1]
        LOG.debug("Converting provided %s option value to dict", param.opts[0])
        return {host: ip}


class RemoteInvokeOutputFormatType(click.Choice):
    """
    Custom Parameter Type for output-format option of remote invoke command.
    """

    def __init__(self, enum):
        self.enum = enum
        super().__init__(choices=[item.name.lower() for item in enum])

    def convert(self, value, param, ctx):
        """Converts the user provided parameter value for the option to
           the provided Enum

        Parameters
        ------------
        value: User provided value for the click option
        param: click parameter
        ctx: Context
        """
        LOG.debug("Converting provided %s option value to Enum", param.opts[0])
        value = super().convert(value, param, ctx)
        return self.enum(value)


class SyncWatchExcludeType(click.ParamType):
    """
    Custom parameter type to parse Key=Value pairs into dictionary mapping.
    """

    name = "list"

    WATCH_EXCLUDE_EXPECTED_LENGTH = 2
    WATCH_EXCLUDE_DELIMITER = "="

    EXCEPTION_MESSAGE = "Argument must be a key, value pair in the format Key=Value."

    def convert(
        self, value: Union[Dict[str, List[str]], str], param: Optional[click.Parameter], ctx: Optional[click.Context]
    ) -> Dict[str, List[str]]:
        """
        Parses the multiple provided values into a mapping of resources to a list of exclusions.

        Parameters
        ----------
        value: Union[Dict[str, List[str]], str]
            The passed in arugment
        param: click.Parameter
            The parameter that was provided
        ctx: click.Context
            The click context

        Returns
        -------
        Dict[str, List[str]]
            A key value pair of Logical/Resource Id and excluded file
        """
        if isinstance(value, dict):
            return value

        mapping_pair = value.split(self.WATCH_EXCLUDE_DELIMITER)

        if len(mapping_pair) != self.WATCH_EXCLUDE_EXPECTED_LENGTH:
            raise click.BadParameter(
                param=param,
                param_hint=f"'{value}'",
                ctx=ctx,
                message=self.EXCEPTION_MESSAGE,
            )

        resource_id, excluded_path = mapping_pair

        if not (resource_id and excluded_path):
            raise click.BadParameter(
                param=param,
                param_hint=f"'{value}'",
                ctx=ctx,
                message=self.EXCEPTION_MESSAGE,
            )

        return {resource_id: [excluded_path]}


class TextWithSpaces:
    def __init__(self, text) -> None:
        self.text = text
        self.modified_text = text
        self.space_positions = []  # type: List[int]

    def replace_spaces(self, replacement="_"):
        """
        Replace spaces in a text with a replacement together with its original locations.
        Input: "test 1"
        Output: "test_1" [4]
        """
        self.space_positions = [i for i, char in enumerate(self.text) if char == " "]
        self.modified_text = self.text.replace(" ", replacement)

        return self.modified_text

    def restore_spaces(self):
        """
        Restore spaces in a text from a original space locations.
        Input: "test_1" [4]
        Output: "test 1"
        """
        text_list = list(self.modified_text)

        for pos in self.space_positions:
            text_list[pos] = " "

        return "".join(text_list)
