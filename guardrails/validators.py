"""This module contains the validators for the Guardrails framework.

The name with which a validator is registered is the name that is used
in the `RAIL` spec to specify formatters.
"""
import ast
import contextvars
import inspect
import itertools
import logging
import os
import re
import string
import warnings
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

import rstr
from tenacity import retry, stop_after_attempt, wait_random_exponential

from guardrails.utils.casting_utils import to_int
from guardrails.utils.docs_utils import get_chunks_from_text, sentence_split
from guardrails.utils.openai_utils import (
    OpenAIClient,
    get_static_openai_chat_create_func,
)
from guardrails.utils.sql_utils import SQLDriver, create_sql_driver
from guardrails.utils.validator_utils import PROVENANCE_V1_PROMPT
from guardrails.validator_base import (
    FailResult,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)

try:
    import numpy as np
except ImportError:
    _HAS_NUMPY = False
else:
    _HAS_NUMPY = True

try:
    import detect_secrets  # type: ignore
except ImportError:
    detect_secrets = None

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
except ImportError:
    AnalyzerEngine = None
    AnonymizerEngine = None

try:
    import nltk  # type: ignore
except ImportError:
    nltk = None  # type: ignore

if nltk is not None:
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt")

try:
    import spacy
except ImportError:
    spacy = None


logger = logging.getLogger(__name__)


# @register_validator('required', 'all')
# class Required(Validator):
#     """Validates that a value is not None."""

#     def validate(self, key: str, value: Any, schema: Union[Dict, List]) -> bool:
#         """Validates that a value is not None."""

#         return value is not None


# @register_validator('description', 'all')
# class Description(Validator):
#     """Validates that a value is not None."""

#     def validate(self, key: str, value: Any, schema: Union[Dict, List]) -> bool:
#         """Validates that a value is not None."""

#         return value is not None


@register_validator(name="pydantic_field_validator", data_type="all")
class PydanticFieldValidator(Validator):
    """Validates a specific field in a Pydantic model with the specified
    validator method.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `pydantic_field_validator`        |
    | Supported data types          | `Any`                             |
    | Programmatic fix              | Override with return value from `field_validator`.   |

    Parameters: Arguments

        field_validator (Callable): A validator for a specific field in a Pydantic model.
    """  # noqa

    override_value_on_pass = True

    def __init__(
        self,
        field_validator: Callable,
        on_fail: Optional[Callable[..., Any]] = None,
        **kwargs,
    ):
        self.field_validator = field_validator
        super().__init__(on_fail, **kwargs)

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        try:
            validated_field = self.field_validator(value)
        except Exception as e:
            return FailResult(
                error_message=str(e),
                fix_value=None,
            )
        return PassResult(
            value_override=validated_field,
        )

    def to_prompt(self, with_keywords: bool = True) -> str:
        return self.field_validator.__func__.__name__


@register_validator(name="valid-range", data_type=["integer", "float", "percentage"])
class ValidRange(Validator):
    """Validates that a value is within a range.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `valid-range`                     |
    | Supported data types          | `integer`, `float`, `percentage`  |
    | Programmatic fix              | Closest value within the range.   |

    Parameters: Arguments
        min: The inclusive minimum value of the range.
        max: The inclusive maximum value of the range.
    """

    def __init__(
        self,
        min: Optional[int] = None,
        max: Optional[int] = None,
        on_fail: Optional[Callable] = None,
    ):
        super().__init__(on_fail=on_fail, min=min, max=max)

        self._min = min
        self._max = max

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        """Validates that a value is within a range."""
        logger.debug(f"Validating {value} is in range {self._min} - {self._max}...")

        val_type = type(value)

        if self._min is not None and value < val_type(self._min):
            return FailResult(
                error_message=f"Value {value} is less than {self._min}.",
                fix_value=self._min,
            )

        if self._max is not None and value > val_type(self._max):
            return FailResult(
                error_message=f"Value {value} is greater than {self._max}.",
                fix_value=self._max,
            )

        return PassResult()


@register_validator(name="valid-choices", data_type="all")
class ValidChoices(Validator):
    """Validates that a value is within the acceptable choices.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `valid-choices`                   |
    | Supported data types          | `all`                             |
    | Programmatic fix              | None                              |

    Parameters: Arguments
        choices: The list of valid choices.
    """

    def __init__(self, choices: List[Any], on_fail: Optional[Callable] = None):
        super().__init__(on_fail=on_fail, choices=choices)
        self._choices = choices

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        """Validates that a value is within a range."""
        logger.debug(f"Validating {value} is in choices {self._choices}...")

        if value not in self._choices:
            return FailResult(
                error_message=f"Value {value} is not in choices {self._choices}.",
            )

        return PassResult()


@register_validator(name="lower-case", data_type="string")
class LowerCase(Validator):
    """Validates that a value is lower case.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `lower-case`                      |
    | Supported data types          | `string`                          |
    | Programmatic fix              | Convert to lower case.            |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is lower case...")

        if value.lower() != value:
            return FailResult(
                error_message=f"Value {value} is not lower case.",
                fix_value=value.lower(),
            )

        return PassResult()


@register_validator(name="upper-case", data_type="string")
class UpperCase(Validator):
    """Validates that a value is upper case.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `upper-case`                      |
    | Supported data types          | `string`                          |
    | Programmatic fix              | Convert to upper case.            |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is upper case...")

        if value.upper() != value:
            return FailResult(
                error_message=f"Value {value} is not upper case.",
                fix_value=value.upper(),
            )

        return PassResult()


@register_validator(name="length", data_type=["string", "list"])
class ValidLength(Validator):
    """Validates that the length of value is within the expected range.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `length`                          |
    | Supported data types          | `string`, `list`, `object`        |
    | Programmatic fix              | If shorter than the minimum, pad with empty last elements. If longer than the maximum, truncate. |

    Parameters: Arguments
        min: The inclusive minimum length.
        max: The inclusive maximum length.
    """  # noqa

    def __init__(
        self,
        min: Optional[int] = None,
        max: Optional[int] = None,
        on_fail: Optional[Callable] = None,
    ):
        super().__init__(on_fail=on_fail, min=min, max=max)
        self._min = to_int(min)
        self._max = to_int(max)

    def validate(self, value: Union[str, List], metadata: Dict) -> ValidationResult:
        """Validates that the length of value is within the expected range."""
        logger.debug(
            f"Validating {value} is in length range {self._min} - {self._max}..."
        )

        if self._min is not None and len(value) < self._min:
            logger.debug(f"Value {value} is less than {self._min}.")

            # Repeat the last character to make the value the correct length.
            if isinstance(value, str):
                if not value:
                    last_val = rstr.rstr(string.ascii_lowercase, 1)
                else:
                    last_val = value[-1]
                corrected_value = value + last_val * (self._min - len(value))
            else:
                if not value:
                    last_val = [rstr.rstr(string.ascii_lowercase, 1)]
                else:
                    last_val = [value[-1]]
                # extend value by padding it out with last_val
                corrected_value = value.extend([last_val] * (self._min - len(value)))

            return FailResult(
                error_message=f"Value has length less than {self._min}. "
                f"Please return a longer output, "
                f"that is shorter than {self._max} characters.",
                fix_value=corrected_value,
            )

        if self._max is not None and len(value) > self._max:
            logger.debug(f"Value {value} is greater than {self._max}.")
            return FailResult(
                error_message=f"Value has length greater than {self._max}. "
                f"Please return a shorter output, "
                f"that is shorter than {self._max} characters.",
                fix_value=value[: self._max],
            )

        return PassResult()


@register_validator(name="regex_match", data_type="string")
class RegexMatch(Validator):
    """Validates that a value matches a regular expression.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `regex_match`                     |
    | Supported data types          | `string`                          |
    | Programmatic fix              | Generate a string that matches the regular expression |

    Parameters: Arguments
        regex: Str regex pattern
        match_type: Str in {"search", "fullmatch"} for a regex search or full-match option
    """  # noqa

    def __init__(
        self,
        regex: str,
        match_type: Optional[str] = None,
        on_fail: Optional[Callable] = None,
    ):
        # todo -> something forces this to be passed as kwargs and therefore xml-ized.
        # match_types = ["fullmatch", "search"]

        if match_type is None:
            match_type = "fullmatch"
        assert match_type in [
            "fullmatch",
            "search",
        ], 'match_type must be in ["fullmatch", "search"]'

        super().__init__(on_fail=on_fail, match_type=match_type, regex=regex)
        self._regex = regex
        self._match_type = match_type

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        p = re.compile(self._regex)
        """Validates that value matches the provided regular expression."""
        # Pad matching string on either side for fix
        # example if we are performing a regex search
        str_padding = (
            "" if self._match_type == "fullmatch" else rstr.rstr(string.ascii_lowercase)
        )
        self._fix_str = str_padding + rstr.xeger(self._regex) + str_padding

        if not getattr(p, self._match_type)(value):
            return FailResult(
                error_message=f"Result must match {self._regex}",
                fix_value=self._fix_str,
            )
        return PassResult()

    def to_prompt(self, with_keywords: bool = True) -> str:
        return "results should match " + self._regex


@register_validator(name="two-words", data_type="string")
class TwoWords(Validator):
    """Validates that a value is two words.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `two-words`                       |
    | Supported data types          | `string`                          |
    | Programmatic fix              | Pick the first two words.         |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is two words...")

        if len(value.split()) != 2:
            return FailResult(
                error_message="must be exactly two words",
                fix_value=" ".join(value.split()[:2]),
            )

        return PassResult()


@register_validator(name="one-line", data_type="string")
class OneLine(Validator):
    """Validates that a value is a single line or sentence.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `one-line`                        |
    | Supported data types          | `string`                          |
    | Programmatic fix              | Pick the first line.              |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is a single line...")

        if len(value.splitlines()) > 1:
            return FailResult(
                error_message=f"Value {value} is not a single line.",
                fix_value=value.splitlines()[0],
            )

        return PassResult()


@register_validator(name="valid-url", data_type=["string"])
class ValidURL(Validator):
    """Validates that a value is a valid URL.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `valid-url`                       |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is a valid URL...")

        from urllib.parse import urlparse

        # Check that the URL is valid
        try:
            result = urlparse(value)
            # Check that the URL has a scheme and network location
            if not result.scheme or not result.netloc:
                return FailResult(
                    error_message=f"URL {value} is not valid.",
                )
        except ValueError:
            return FailResult(
                error_message=f"URL {value} is not valid.",
            )

        return PassResult()


@register_validator(name="is-reachable", data_type=["string"])
class EndpointIsReachable(Validator):
    """Validates that a value is a reachable URL.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `is-reachable`                    |
    | Supported data types          | `string`,                         |
    | Programmatic fix              | None                              |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is a valid URL...")

        import requests

        # Check that the URL exists and can be reached
        try:
            response = requests.get(value)
            if response.status_code != 200:
                return FailResult(
                    error_message=f"URL {value} returned "
                    f"status code {response.status_code}",
                )
        except requests.exceptions.ConnectionError:
            return FailResult(
                error_message=f"URL {value} could not be reached",
            )
        except requests.exceptions.InvalidSchema:
            return FailResult(
                error_message=f"URL {value} does not specify "
                f"a valid connection adapter",
            )
        except requests.exceptions.MissingSchema:
            return FailResult(
                error_message=f"URL {value} does not contain " f"a http schema",
            )

        return PassResult()


@register_validator(name="bug-free-python", data_type="string")
class BugFreePython(Validator):
    """Validates that there are no Python syntactic bugs in the generated code.

    This validator checks for syntax errors by running `ast.parse(code)`,
    and will raise an exception if there are any.
    Only the packages in the `python` environment are available to the code snippet.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `bug-free-python`                 |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is not a bug...")

        # The value is a Python code snippet. We need to check for syntax errors.
        try:
            ast.parse(value)
        except SyntaxError as e:
            return FailResult(
                error_message=f"Syntax error: {e.msg}",
            )

        return PassResult()


@register_validator(name="bug-free-sql", data_type=["string"])
class BugFreeSQL(Validator):
    """Validates that there are no SQL syntactic bugs in the generated code.

    This is a very minimal implementation that uses the Pypi `sqlvalidator` package
    to check if the SQL query is valid. You can implement a custom SQL validator
    that uses a database connection to check if the query is valid.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `bug-free-sql`                    |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |
    """

    def __init__(
        self,
        conn: Optional[str] = None,
        schema_file: Optional[str] = None,
        on_fail: Optional[Callable] = None,
    ):
        super().__init__(on_fail=on_fail)
        self._driver: SQLDriver = create_sql_driver(schema_file=schema_file, conn=conn)

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        errors = self._driver.validate_sql(value)
        if len(errors) > 0:
            return FailResult(
                error_message=". ".join(errors),
            )

        return PassResult()


@register_validator(name="sql-column-presence", data_type="string")
class SqlColumnPresence(Validator):
    """Validates that all columns in the SQL query are present in the schema.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `sql-column-presence`             |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |

    Parameters: Arguments
        cols: The list of valid columns.
    """

    def __init__(self, cols: List[str], on_fail: Optional[Callable] = None):
        super().__init__(on_fail=on_fail, cols=cols)
        self._cols = set(cols)

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        from sqlglot import exp, parse

        expressions = parse(value)
        cols = set()
        for expression in expressions:
            if expression is None:
                continue
            for col in expression.find_all(exp.Column):
                cols.add(col.alias_or_name)

        diff = cols.difference(self._cols)
        if len(diff) > 0:
            return FailResult(
                error_message=f"Columns [{', '.join(diff)}] "
                f"not in [{', '.join(self._cols)}]",
            )

        return PassResult()


@register_validator(name="exclude-sql-predicates", data_type="string")
class ExcludeSqlPredicates(Validator):
    """Validates that the SQL query does not contain certain predicates.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `exclude-sql-predicates`          |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |

    Parameters: Arguments
        predicates: The list of predicates to avoid.
    """

    def __init__(self, predicates: List[str], on_fail: Optional[Callable] = None):
        super().__init__(on_fail=on_fail, predicates=predicates)
        self._predicates = set(predicates)

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        from sqlglot import exp, parse

        expressions = parse(value)
        for expression in expressions:
            if expression is None:
                continue
            for pred in self._predicates:
                try:
                    getattr(exp, pred)
                except AttributeError:
                    raise ValueError(f"Predicate {pred} does not exist")
                if len(list(expression.find_all(getattr(exp, pred)))):
                    return FailResult(
                        error_message=f"SQL query contains predicate {pred}",
                        fix_value="",
                    )

        return PassResult()


@register_validator(name="similar-to-document", data_type="string")
class SimilarToDocument(Validator):
    """Validates that a value is similar to the document.

    This validator checks if the value is similar to the document by checking
    the cosine similarity between the value and the document, using an
    embedding.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `similar-to-document`             |
    | Supported data types          | `string`                             |
    | Programmatic fix              | None                              |

    Parameters: Arguments
        document: The document to use for the similarity check.
        threshold: The minimum cosine similarity to be considered similar.  Defaults to 0.7.
        model: The embedding model to use.  Defaults to text-embedding-ada-002.
    """  # noqa

    def __init__(
        self,
        document: str,
        threshold: float = 0.7,
        model: str = "text-embedding-ada-002",
        on_fail: Optional[Callable] = None,
    ):
        super().__init__(
            on_fail=on_fail, document=document, threshold=threshold, model=model
        )
        if not _HAS_NUMPY:
            raise ImportError(
                f"The {self.__class__.__name__} validator requires the numpy package.\n"
                "`poetry add numpy` to install it."
            )

        self.client = OpenAIClient()

        self._document = document
        embedding_response = self.client.create_embedding(input=[document], model=model)
        embedding = embedding_response[0]  # type: ignore
        self._document_embedding = np.array(embedding)
        self._model = model
        self._threshold = float(threshold)

    @staticmethod
    def cosine_similarity(a: "np.ndarray", b: "np.ndarray") -> float:
        """Calculate the cosine similarity between two vectors.

        Args:
            a: The first vector.
            b: The second vector.

        Returns:
            float: The cosine similarity between the two vectors.
        """
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} is similar to document...")

        embedding_response = self.client.create_embedding(
            input=[value], model=self._model
        )

        value_embedding = np.array(embedding_response[0])  # type: ignore

        similarity = self.cosine_similarity(
            self._document_embedding,
            value_embedding,
        )
        if similarity < self._threshold:
            return FailResult(
                error_message=f"Value {value} is not similar enough "
                f"to document {self._document}.",
            )

        return PassResult()

    def to_prompt(self, with_keywords: bool = True) -> str:
        return ""


@register_validator(name="is-profanity-free", data_type="string")
class IsProfanityFree(Validator):
    """Validates that a translated text does not contain profanity language.

    This validator uses the `alt-profanity-check` package to check if a string
    contains profanity language.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `is-profanity-free`               |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |
    """

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        try:
            from profanity_check import predict  # type: ignore
        except ImportError:
            raise ImportError(
                "`is-profanity-free` validator requires the `alt-profanity-check`"
                "package. Please install it with `poetry add profanity-check`."
            )

        prediction = predict([value])
        if prediction[0] == 1:
            return FailResult(
                error_message=f"{value} contains profanity. "
                f"Please return a profanity-free output.",
                fix_value="",
            )
        return PassResult()


@register_validator(name="is-high-quality-translation", data_type="string")
class IsHighQualityTranslation(Validator):
    """Using inpiredco.critique to check if a translation is high quality.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `is-high-quality-translation`     |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |

    Other parameters: Metadata
        translation_source (str): The source of the translation.
    """

    required_metadata_keys = ["translation_source"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from inspiredco.critique import Critique  # type: ignore

            inspiredco_api_key = os.environ.get("INSPIREDCO_API_KEY")
            if not inspiredco_api_key:
                raise ValueError(
                    "The INSPIREDCO_API_KEY environment variable must be set"
                    "in order to use the is-high-quality-translation validator!"
                )

            self._critique = Critique(api_key=inspiredco_api_key)

        except ImportError:
            raise ImportError(
                "`is-high-quality-translation` validator requires the `inspiredco`"
                "package. Please install it with `poetry add inspiredco`."
            )

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        if "translation_source" not in metadata:
            raise RuntimeError(
                "is-high-quality-translation validator expects "
                "`translation_source` key in metadata"
            )
        src = metadata["translation_source"]
        prediction = self._critique.evaluate(
            metric="comet",
            config={"model": "unbabel_comet/wmt21-comet-qe-da"},
            dataset=[{"source": src, "target": value}],
        )
        quality = prediction["examples"][0]["value"]
        if quality < -0.1:
            return FailResult(
                error_message=f"{value} is a low quality translation."
                "Please return a higher quality output.",
                fix_value="",
            )
        return PassResult()


@register_validator(name="ends-with", data_type="list")
class EndsWith(Validator):
    """Validates that a list ends with a given value.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `ends-with`                       |
    | Supported data types          | `list`                            |
    | Programmatic fix              | Append the given value to the list. |

    Parameters: Arguments
        end: The required last element.
    """

    def __init__(self, end: str, on_fail: str = "fix"):
        super().__init__(on_fail=on_fail, end=end)
        self._end = end

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(f"Validating {value} ends with {self._end}...")

        if not value[-1] == self._end:
            return FailResult(
                error_message=f"{value} must end with {self._end}",
                fix_value=value + [self._end],
            )

        return PassResult()


@register_validator(name="extracted-summary-sentences-match", data_type="string")
class ExtractedSummarySentencesMatch(Validator):
    """Validates that the extracted summary sentences match the original text
    by performing a cosine similarity in the embedding space.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `extracted-summary-sentences-match` |
    | Supported data types          | `string`                            |
    | Programmatic fix              | Remove any sentences that can not be verified. |

    Parameters: Arguments

        threshold: The minimum cosine similarity to be considered similar. Default to 0.7.

    Other parameters: Metadata

        filepaths (List[str]): A list of strings that specifies the filepaths for any documents that should be used for asserting the summary's similarity.
        document_store (DocumentStoreBase, optional): The document store to use during validation. Defaults to EphemeralDocumentStore.
        vector_db (VectorDBBase, optional): A vector database to use for embeddings.  Defaults to Faiss.
        embedding_model (EmbeddingBase, optional): The embeddig model to use. Defaults to OpenAIEmbedding.
    """  # noqa

    required_metadata_keys = ["filepaths"]

    def __init__(
        self,
        threshold: float = 0.7,
        on_fail: Optional[Callable] = None,
        **kwargs: Optional[Dict[str, Any]],
    ):
        super().__init__(on_fail, **kwargs)
        # TODO(shreya): Pass embedding_model, vector_db, document_store from spec

        self._threshold = float(threshold)

    @staticmethod
    def _instantiate_store(
        metadata, api_key: Optional[str] = None, api_base: Optional[str] = None
    ):
        if "document_store" in metadata:
            return metadata["document_store"]

        from guardrails.document_store import EphemeralDocumentStore

        if "vector_db" in metadata:
            vector_db = metadata["vector_db"]
        else:
            from guardrails.vectordb import Faiss

            if "embedding_model" in metadata:
                embedding_model = metadata["embedding_model"]
            else:
                from guardrails.embedding import OpenAIEmbedding

                embedding_model = OpenAIEmbedding(api_key=api_key, api_base=api_base)

            vector_db = Faiss.new_flat_ip_index(
                embedding_model.output_dim, embedder=embedding_model
            )

        return EphemeralDocumentStore(vector_db)

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        if "filepaths" not in metadata:
            raise RuntimeError(
                "extracted-sentences-summary-match validator expects "
                "`filepaths` key in metadata"
            )
        filepaths = metadata["filepaths"]

        kwargs = {}
        context_copy = contextvars.copy_context()
        for key, context_var in context_copy.items():
            if key.name == "kwargs" and isinstance(kwargs, dict):
                kwargs = context_var
                break

        api_key = kwargs.get("api_key")
        api_base = kwargs.get("api_base")

        store = self._instantiate_store(metadata, api_key, api_base)

        sources = []
        for filepath in filepaths:
            with open(filepath) as f:
                doc = f.read()
                store.add_text(doc, {"path": filepath})
                sources.append(filepath)

        # Split the value into sentences.
        sentences = re.split(r"(?<=[.!?]) +", value)

        # Check if any of the sentences in the value match any of the sentences
        # in the documents.
        unverified = []
        verified = []
        citations = {}
        for id_, sentence in enumerate(sentences):
            page = store.search_with_threshold(sentence, self._threshold)
            if not page or page[0].metadata["path"] not in sources:
                unverified.append(sentence)
            else:
                sentence_id = id_ + 1
                citation_path = page[0].metadata["path"]
                citation_id = sources.index(citation_path) + 1

                citations[sentence_id] = citation_id
                verified.append(sentence + f" [{citation_id}]")

        fixed_summary = (
            " ".join(verified)
            + "\n\n"
            + "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(sources))
        )
        metadata["summary_with_citations"] = fixed_summary
        metadata["citations"] = citations

        if unverified:
            unverified_sentences = "\n".join(unverified)
            return FailResult(
                metadata=metadata,
                error_message=(
                    f"The summary \nSummary: {value}\n has sentences\n"
                    f"{unverified_sentences}\n that are not similar to any document."
                ),
                fix_value=fixed_summary,
            )

        return PassResult(metadata=metadata)

    def to_prompt(self, with_keywords: bool = True) -> str:
        return ""


@register_validator(name="reading-time", data_type="string")
class ReadingTime(Validator):
    """Validates that the a string can be read in less than a certain amount of
    time.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `reading-time`                      |
    | Supported data types          | `string`                            |
    | Programmatic fix              | None                                |

    Parameters: Arguments

        reading_time: The maximum reading time.
    """

    def __init__(self, reading_time: int, on_fail: str = "fix"):
        super().__init__(on_fail=on_fail, reading_time=reading_time)
        self._max_time = reading_time

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        logger.debug(
            f"Validating {value} can be read in less than {self._max_time} seconds..."
        )

        # Estimate the reading time of the string
        reading_time = len(value.split()) / 200 * 60
        logger.debug(f"Estimated reading time {reading_time} seconds...")

        if abs(reading_time - self._max_time) > 1:
            logger.error(f"{value} took {reading_time} to read")
            return FailResult(
                error_message=f"String should be readable "
                f"within {self._max_time} minutes.",
                fix_value=value,
            )

        return PassResult()


@register_validator(name="extractive-summary", data_type="string")
class ExtractiveSummary(Validator):
    """Validates that a string is a valid extractive summary of a given
    document.

    This validator does a fuzzy match between the sentences in the
    summary and the sentences in the document. Each sentence in the
    summary must be similar to at least one sentence in the document.
    After the validation, the summary is updated to include the
    sentences from the document that were matched, and the citations for
    those sentences are added to the end of the summary.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `extractive-summary`                |
    | Supported data types          | `string`                            |
    | Programmatic fix              | Remove any sentences that can not be verified. |

    Parameters: Arguments

        threshold: The minimum fuzz ratio to be considered summarized.  Defaults to 85.

    Other parameters: Metadata

        filepaths (List[str]): A list of strings that specifies the filepaths for any documents that should be used for asserting the summary's similarity.
    """  # noqa

    required_metadata_keys = ["filepaths"]

    def __init__(
        self,
        threshold: int = 85,
        on_fail: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(on_fail, **kwargs)

        self._threshold = threshold

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        """Make sure each sentence was precisely copied from the document."""

        if "filepaths" not in metadata:
            raise RuntimeError(
                "extractive-summary validator expects " "`filepaths` key in metadata"
            )

        filepaths = metadata["filepaths"]

        # Load documents
        store = {}
        for filepath in filepaths:
            with open(filepath) as f:
                doc = f.read()
            store[filepath] = sentence_split(doc)

        try:
            from thefuzz import fuzz  # type: ignore
        except ImportError:
            raise ImportError(
                "`thefuzz` library is required for `extractive-summary` validator. "
                "Please install it with `poetry add thefuzz`."
            )

        # Split the value into sentences.
        sentences = sentence_split(value)

        # Check if any of the sentences in the value match any of the sentences
        # # in the documents.
        unverified = []
        verified = []
        citations = {}

        for id_, sentence in enumerate(sentences):
            highest_ratio = 0
            highest_ratio_doc = None

            # Check fuzzy match against all sentences in all documents
            for doc_path, doc_sentences in store.items():
                for doc_sentence in doc_sentences:
                    ratio = fuzz.ratio(sentence, doc_sentence)
                    if ratio > highest_ratio:
                        highest_ratio = ratio
                        highest_ratio_doc = doc_path

            if highest_ratio < self._threshold:
                unverified.append(sentence)
            else:
                sentence_id = id_ + 1
                citation_id = list(store).index(highest_ratio_doc) + 1

                citations[sentence_id] = citation_id
                verified.append(sentence + f" [{citation_id}]")

        verified_sentences = (
            " ".join(verified)
            + "\n\n"
            + "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(store))
        )

        metadata["summary_with_citations"] = verified_sentences
        metadata["citations"] = citations

        if len(unverified):
            unverified_sentences = "\n".join(
                "- " + s for i, s in enumerate(sentences) if i in unverified
            )
            return FailResult(
                metadata=metadata,
                error_message=(
                    f"The summary \nSummary: {value}\n has sentences\n"
                    f"{unverified_sentences}\n that are not similar to any document."
                ),
                fix_value="\n".join(verified_sentences),
            )

        return PassResult(
            metadata=metadata,
        )


@register_validator(name="remove-redundant-sentences", data_type="string")
class RemoveRedundantSentences(Validator):
    """Removes redundant sentences from a string.

    This validator removes sentences from a string that are similar to
    other sentences in the string. This is useful for removing
    repetitive sentences from a string.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `remove-redundant-sentences`        |
    | Supported data types          | `string`                            |
    | Programmatic fix              | Remove any redundant sentences.     |

    Parameters: Arguments

        threshold: The minimum fuzz ratio to be considered redundant.  Defaults to 70.
    """

    def __init__(
        self, threshold: int = 70, on_fail: Optional[Callable] = None, **kwargs
    ):
        super().__init__(on_fail, **kwargs)
        self._threshold = threshold

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        """Remove redundant sentences from a string."""

        try:
            from thefuzz import fuzz  # type: ignore
        except ImportError:
            raise ImportError(
                "`thefuzz` library is required for `remove-redundant-sentences` "
                "validator. Please install it with `poetry add thefuzz`."
            )

        # Split the value into sentences.
        sentences = sentence_split(value)
        filtered_sentences = []
        redundant_sentences = []

        sentence = sentences[0]
        other_sentences = sentences[1:]
        while len(other_sentences):
            # Check fuzzy match against all other sentences
            filtered_sentences.append(sentence)
            unique_sentences = []
            for other_sentence in other_sentences:
                ratio = fuzz.ratio(sentence, other_sentence)
                if ratio > self._threshold:
                    redundant_sentences.append(other_sentence)
                else:
                    unique_sentences.append(other_sentence)
            if len(unique_sentences) == 0:
                break
            sentence = unique_sentences[0]
            other_sentences = unique_sentences[1:]

        filtered_summary = " ".join(filtered_sentences)

        if len(redundant_sentences):
            redundant_sentences = "\n".join(redundant_sentences)
            return FailResult(
                error_message=(
                    f"The summary \nSummary: {value}\n has sentences\n"
                    f"{redundant_sentences}\n that are similar to other sentences."
                ),
                fix_value=filtered_summary,
            )

        return PassResult()


@register_validator(name="saliency-check", data_type="string")
class SaliencyCheck(Validator):
    """Checks that the summary covers the list of topics present in the
    document.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `saliency-check`                    |
    | Supported data types          | `string`                            |
    | Programmatic fix              | None                                |

    Parameters: Arguments

        docs_dir: Path to the directory containing the documents.
        threshold: Threshold for overlap between topics in document and summary. Defaults to 0.25
    """  # noqa

    def __init__(
        self,
        docs_dir: str,
        llm_callable: Optional[Callable] = None,
        on_fail: Optional[Callable] = None,
        threshold: float = 0.25,
        **kwargs,
    ):
        """Initialize the SalienceCheck validator.

        Args:
            docs_dir: Path to the directory containing the documents.
            on_fail: Function to call when validation fails.
            threshold: Threshold for overlap between topics in document and summary.
        """

        super().__init__(on_fail, **kwargs)

        if llm_callable is not None and inspect.iscoroutinefunction(llm_callable):
            raise ValueError(
                "SaliencyCheck validator does not support async LLM callables."
            )

        self.llm_callable = (
            llm_callable if llm_callable else get_static_openai_chat_create_func()
        )

        self._threshold = threshold

        # Load documents
        self._document_store = {}
        for doc_path in os.listdir(docs_dir):
            with open(os.path.join(docs_dir, doc_path)) as f:
                text = f.read()
            # Precompute topics for each document
            self._document_store[doc_path] = self._get_topics(text)

    @property
    def _topics(self) -> List[str]:
        """Return a list of topics that can be used in the validator."""
        # Merge topics from all documents
        topics = set()
        for doc_topics in self._document_store.values():
            topics.update(doc_topics)
        return list(topics)

    def _get_topics(self, text: str, topics: Optional[List[str]] = None) -> List[str]:
        """Extract topics from a string."""

        from guardrails import Guard

        topics_seed = ""
        if topics is not None:
            topics_seed = (
                "Here's a seed list of topics, select topics from this list"
                " if they are covered in the doc:\n\n" + ", ".join(topics)
            )

        spec = f"""
<rail version="0.1">
<output>
    <list name="topics">
        <string name="topic" description="few words describing the topic in text"/>
    </list>
</output>

<prompt>
Extract a list of topics from the following text:

{text}

{topics_seed}

Return the output as a JSON with a single key "topics" containing a list of topics.

Make sure that topics are relevant to text, and topics are not too specific or general.
</prompt>
</rail>
    """

        guard = Guard.from_rail_string(spec)
        _, validated_output = guard(llm_api=self.llm_callable)  # type: ignore
        return validated_output["topics"]

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        topics_in_summary = self._get_topics(value, topics=self._topics)

        # Compute overlap between topics in document and summary
        intersection = set(topics_in_summary).intersection(set(self._topics))
        overlap = len(intersection) / len(self._topics)

        if overlap < self._threshold:
            return FailResult(
                error_message=(
                    f"The summary \nSummary: {value}\n does not cover these topics:\n"
                    f"{set(self._topics).difference(intersection)}"
                ),
                fix_value="",
            )

        return PassResult()


@register_validator(name="qa-relevance-llm-eval", data_type="string")
class QARelevanceLLMEval(Validator):
    """Validates that an answer is relevant to the question asked by asking the
    LLM to self evaluate.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `qa-relevance-llm-eval`             |
    | Supported data types          | `string`                            |
    | Programmatic fix              | None                                |

    Other parameters: Metadata
        question (str): The original question the llm was given to answer.
    """

    required_metadata_keys = ["question"]

    def __init__(
        self,
        llm_callable: Optional[Callable] = None,
        on_fail: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(on_fail, **kwargs)

        if llm_callable is not None and inspect.iscoroutinefunction(llm_callable):
            raise ValueError(
                "QARelevanceLLMEval validator does not support async LLM callables."
            )

        self.llm_callable = (
            llm_callable if llm_callable else get_static_openai_chat_create_func()
        )

    def _selfeval(self, question: str, answer: str):
        from guardrails import Guard

        spec = """
<rail version="0.1">
<output>
    <bool name="relevant" />
</output>

<prompt>
Is the answer below relevant to the question asked?
Question: {question}
Answer: {answer}

Relevant (as a JSON with a single boolean key, "relevant"):\
</prompt>
</rail>
    """.format(
            question=question,
            answer=answer,
        )
        guard = Guard.from_rail_string(spec)

        _, validated_output = guard(
            self.llm_callable,  # type: ignore
            max_tokens=10,
            temperature=0.1,
        )
        return validated_output

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        if "question" not in metadata:
            raise RuntimeError(
                "qa-relevance-llm-eval validator expects " "`question` key in metadata"
            )

        question = metadata["question"]

        relevant = self._selfeval(question, value)["relevant"]
        if relevant:
            return PassResult()

        fixed_answer = "No relevant answer found."
        return FailResult(
            error_message=f"The answer {value} is not relevant "
            f"to the question {question}.",
            fix_value=fixed_answer,
        )

    def to_prompt(self, with_keywords: bool = True) -> str:
        return ""


@register_validator(name="provenance-v0", data_type="string")
class ProvenanceV0(Validator):
    """Validates that LLM-generated text matches some source text based on
    distance in embedding space.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `provenance-v0`                     |
    | Supported data types          | `string`                            |
    | Programmatic fix              | None                                |

    Parameters: Arguments
        threshold: The minimum cosine similarity between the generated text and
            the source text. Defaults to 0.8.
        validation_method: Whether to validate at the sentence level or over the full text.  Must be one of `sentence` or `full`. Defaults to `sentence`

    Other parameters: Metadata
        query_function (Callable, optional): A callable that takes a string and returns a list of (chunk, score) tuples.
        sources (List[str], optional): The source text.
        embed_function (Callable, optional): A callable that creates embeddings for the sources. Must accept a list of strings and return an np.array of floats.

    In order to use this validator, you must provide either a `query_function` or
    `sources` with an `embed_function` in the metadata.

    If providing query_function, it should take a string as input and return a list of
    (chunk, score) tuples. The chunk is a string and the score is a float representing
    the cosine distance between the chunk and the input string. The list should be
    sorted in ascending order by score.

    Note: The score should represent distance in embedding space, not similarity. I.e.,
    lower is better and the score should be 0 if the chunk is identical to the input
    string.

    Example:
        ```py
        def query_function(text: str, k: int) -> List[Tuple[str, float]]:
            return [("This is a chunk", 0.9), ("This is another chunk", 0.8)]

        guard = Guard.from_rail(...)
        guard(
            openai.ChatCompletion.create(...),
            prompt_params={...},
            temperature=0.0,
            metadata={"query_function": query_function},
        )
        ```


    If providing sources, it should be a list of strings. The embed_function should
    take a string or a list of strings as input and return a np array of floats.
    The vector should be normalized to unit length.

    Example:
        ```py
        def embed_function(text: Union[str, List[str]]) -> np.ndarray:
            return np.array([[0.1, 0.2, 0.3]])

        guard = Guard.from_rail(...)
        guard(
            openai.ChatCompletion.create(...),
            prompt_params={...},
            temperature=0.0,
            metadata={
                "sources": ["This is a source text"],
                "embed_function": embed_function
            },
        )
        ```
    """  # noqa

    def __init__(
        self,
        threshold: float = 0.8,
        validation_method: str = "sentence",
        on_fail: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(
            on_fail, threshold=threshold, validation_method=validation_method, **kwargs
        )
        self._threshold = float(threshold)
        if validation_method not in ["sentence", "full"]:
            raise ValueError("validation_method must be 'sentence' or 'full'.")
        self._validation_method = validation_method

    def get_query_function(self, metadata: Dict[str, Any]) -> Callable:
        query_fn = metadata.get("query_function", None)
        sources = metadata.get("sources", None)

        # Check that query_fn or sources are provided
        if query_fn is not None:
            if sources is not None:
                warnings.warn(
                    "Both `query_function` and `sources` are provided in metadata. "
                    "`query_function` will be used."
                )
            return query_fn

        if sources is None:
            raise ValueError(
                "You must provide either `query_function` or `sources` in metadata."
            )

        # Check chunking strategy
        chunk_strategy = metadata.get("chunk_strategy", "sentence")
        if chunk_strategy not in ["sentence", "word", "char", "token"]:
            raise ValueError(
                "`chunk_strategy` must be one of 'sentence', 'word', 'char', "
                "or 'token'."
            )
        chunk_size = metadata.get("chunk_size", 5)
        chunk_overlap = metadata.get("chunk_overlap", 2)

        # Check distance metric
        distance_metric = metadata.get("distance_metric", "cosine")
        if distance_metric not in ["cosine", "euclidean"]:
            raise ValueError(
                "`distance_metric` must be one of 'cosine' or 'euclidean'."
            )

        # Check embed model
        embed_function = metadata.get("embed_function", None)
        if embed_function is None:
            raise ValueError(
                "You must provide `embed_function` in metadata in order to "
                "use the default query function."
            )
        return partial(
            self.query_vector_collection,
            sources=metadata["sources"],
            chunk_strategy=chunk_strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            distance_metric=distance_metric,
            embed_function=embed_function,
        )

    def validate_each_sentence(
        self, value: Any, query_function: Callable, metadata: Dict[str, Any]
    ) -> ValidationResult:
        if nltk is None:
            raise ImportError(
                "`nltk` library is required for `provenance-v0` validator. "
                "Please install it with `poetry add nltk`."
            )
        # Split the value into sentences using nltk sentence tokenizer.
        sentences = nltk.sent_tokenize(value)

        unsupported_sentences = []
        supported_sentences = []
        for sentence in sentences:
            most_similar_chunks = query_function(text=sentence, k=1)
            if most_similar_chunks is None:
                unsupported_sentences.append(sentence)
                continue
            most_similar_chunk = most_similar_chunks[0]
            if most_similar_chunk[1] < self._threshold:
                supported_sentences.append((sentence, most_similar_chunk[0]))
            else:
                unsupported_sentences.append(sentence)

        metadata["unsupported_sentences"] = "- " + "\n- ".join(unsupported_sentences)
        metadata["supported_sentences"] = supported_sentences
        if unsupported_sentences:
            unsupported_sentences = "- " + "\n- ".join(unsupported_sentences)
            return FailResult(
                metadata=metadata,
                error_message=(
                    f"None of the following sentences in your response are supported "
                    "by provided context:"
                    f"\n{metadata['unsupported_sentences']}"
                ),
                fix_value="\n".join(s[0] for s in supported_sentences),
            )
        return PassResult(metadata=metadata)

    def validate_full_text(
        self, value: Any, query_function: Callable, metadata: Dict[str, Any]
    ) -> ValidationResult:
        most_similar_chunks = query_function(text=value, k=1)
        if most_similar_chunks is None:
            metadata["unsupported_text"] = value
            metadata["supported_text_citations"] = {}
            return FailResult(
                metadata=metadata,
                error_message=(
                    "The following text in your response is not supported by the "
                    "supported by the provided context:\n" + value
                ),
            )
        most_similar_chunk = most_similar_chunks[0]
        if most_similar_chunk[1] > self._threshold:
            metadata["unsupported_text"] = value
            metadata["supported_text_citations"] = {}
            return FailResult(
                metadata=metadata,
                error_message=(
                    "The following text in your response is not supported by the "
                    "supported by the provided context:\n" + value
                ),
            )

        metadata["unsupported_text"] = ""
        metadata["supported_text_citations"] = {
            value: most_similar_chunk[0],
        }
        return PassResult(metadata=metadata)

    def validate(self, value: Any, metadata: Dict[str, Any]) -> ValidationResult:
        query_function = self.get_query_function(metadata)

        if self._validation_method == "sentence":
            return self.validate_each_sentence(value, query_function, metadata)
        elif self._validation_method == "full":
            return self.validate_full_text(value, query_function, metadata)
        else:
            raise ValueError("validation_method must be 'sentence' or 'full'.")

    @staticmethod
    def query_vector_collection(
        text: str,
        k: int,
        sources: List[str],
        embed_function: Callable,
        chunk_strategy: str = "sentence",
        chunk_size: int = 5,
        chunk_overlap: int = 2,
        distance_metric: str = "cosine",
    ) -> List[Tuple[str, float]]:
        chunks = [
            get_chunks_from_text(source, chunk_strategy, chunk_size, chunk_overlap)
            for source in sources
        ]
        chunks = list(itertools.chain.from_iterable(chunks))

        # Create embeddings
        source_embeddings = np.array(embed_function(chunks)).squeeze()
        query_embedding = embed_function(text).squeeze()

        # Compute distances
        if distance_metric == "cosine":
            if not _HAS_NUMPY:
                raise ValueError(
                    "You must install numpy in order to use the cosine distance "
                    "metric."
                )

            cos_sim = 1 - (
                np.dot(source_embeddings, query_embedding)
                / (
                    np.linalg.norm(source_embeddings, axis=1)
                    * np.linalg.norm(query_embedding)
                )
            )
            top_indices = np.argsort(cos_sim)[:k]
            top_similarities = [cos_sim[j] for j in top_indices]
            top_chunks = [chunks[j] for j in top_indices]
        else:
            raise ValueError("distance_metric must be 'cosine'.")

        return list(zip(top_chunks, top_similarities))

    def to_prompt(self, with_keywords: bool = True) -> str:
        return ""


@register_validator(name="provenance-v1", data_type="string")
class ProvenanceV1(Validator):
    """Validates that the LLM-generated text is supported by the provided
    contexts.

    This validator uses an LLM callable to evaluate the generated text against the
    provided contexts (LLM-ception).

    In order to use this validator, you must provide either:
    1. a 'query_function' in the metadata. That function should take a string as input
        (the LLM-generated text) and return a list of relevant
    chunks. The list should be sorted in ascending order by the distance between the
        chunk and the LLM-generated text.

    Example using str callable:
        >>> def query_function(text: str, k: int) -> List[str]:
        ...     return ["This is a chunk", "This is another chunk"]

        >>> guard = Guard.from_string(validators=[
                    ProvenanceV1(llm_callable="gpt-3.5-turbo", ...)
                ]
            )
        >>> guard.parse(
        ...   llm_output=...,
        ...   metadata={"query_function": query_function}
        ... )

    Example using a custom llm callable:
        >>> def query_function(text: str, k: int) -> List[str]:
        ...     return ["This is a chunk", "This is another chunk"]

        >>> guard = Guard.from_string(validators=[
                    ProvenanceV1(llm_callable=your_custom_callable, ...)
                ]
            )
        >>> guard.parse(
        ...   llm_output=...,
        ...   metadata={"query_function": query_function}
        ... )

    OR

    2. `sources` with an `embed_function` in the metadata. The embed_function should
        take a string or a list of strings as input and return a np array of floats.
    The vector should be normalized to unit length.

    Example:
        ```py
        def embed_function(text: Union[str, List[str]]) -> np.ndarray:
            return np.array([[0.1, 0.2, 0.3]])

        guard = Guard.from_rail(...)
        guard(
            openai.ChatCompletion.create(...),
            prompt_params={...},
            temperature=0.0,
            metadata={
                "sources": ["This is a source text"],
                "embed_function": embed_function
            },
        )
    """

    def __init__(
        self,
        validation_method: str = "sentence",
        llm_callable: Union[str, Callable] = "gpt-3.5-turbo",
        top_k: int = 3,
        max_tokens: int = 2,
        on_fail: Optional[Callable] = None,
        **kwargs,
    ):
        """
        args:
            validation_method (str): Whether to validate at the sentence level or over
                the full text.  One of `sentence` or `full`. Defaults to `sentence`
            llm_callable (Union[str, Callable]): Either the name of the OpenAI model,
                or a callable that takes a prompt and returns a response.
            top_k (int): The number of chunks to return from the query function.
                Defaults to 3.
            max_tokens (int): The maximum number of tokens to send to the LLM.
                Defaults to 2.

        Other args: Metadata
            query_function (Callable): A callable that takes a string and returns a
                list of chunks.
            sources (List[str], optional): The source text.
            embed_function (Callable, optional): A callable that creates embeddings for
                the sources. Must accept a list of strings and returns float np.array.
        """
        super().__init__(
            on_fail,
            validation_method=validation_method,
            llm_callable=llm_callable,
            top_k=top_k,
            max_tokens=max_tokens,
            **kwargs,
        )
        if validation_method not in ["sentence", "full"]:
            raise ValueError("validation_method must be 'sentence' or 'full'.")
        self._validation_method = validation_method
        self.set_callable(llm_callable)
        self._top_k = int(top_k)
        self._max_tokens = int(max_tokens)

        self.client = OpenAIClient()

    def set_callable(self, llm_callable: Union[str, Callable]) -> None:
        """Set the LLM callable.

        Args:
            llm_callable: Either the name of the OpenAI model, or a callable that takes
                a prompt and returns a response.
        """
        if isinstance(llm_callable, str):
            if llm_callable not in ["gpt-3.5-turbo", "gpt-4"]:
                raise ValueError(
                    "llm_callable must be one of 'gpt-3.5-turbo' or 'gpt-4'."
                    "If you want to use a custom LLM, please provide a callable."
                    "Check out ProvenanceV1 documentation for an example."
                )

            def openai_callable(prompt: str) -> str:
                response = self.client.create_chat_completion(
                    model=llm_callable,
                    messages=[
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=self._max_tokens,
                )
                return response.output

            self._llm_callable = openai_callable
        elif isinstance(llm_callable, Callable):
            self._llm_callable = llm_callable
        else:
            raise ValueError(
                "llm_callable must be either a string or a callable that takes a string"
                " and returns a string."
            )

    def get_query_function(self, metadata: Dict[str, Any]) -> Callable:
        # Exact same as ProvenanceV0

        query_fn = metadata.get("query_function", None)
        sources = metadata.get("sources", None)

        # Check that query_fn or sources are provided
        if query_fn is not None:
            if sources is not None:
                warnings.warn(
                    "Both `query_function` and `sources` are provided in metadata. "
                    "`query_function` will be used."
                )
            return query_fn

        if sources is None:
            raise ValueError(
                "You must provide either `query_function` or `sources` in metadata."
            )

        # Check chunking strategy
        chunk_strategy = metadata.get("chunk_strategy", "sentence")
        if chunk_strategy not in ["sentence", "word", "char", "token"]:
            raise ValueError(
                "`chunk_strategy` must be one of 'sentence', 'word', 'char', "
                "or 'token'."
            )
        chunk_size = metadata.get("chunk_size", 5)
        chunk_overlap = metadata.get("chunk_overlap", 2)

        # Check distance metric
        distance_metric = metadata.get("distance_metric", "cosine")
        if distance_metric not in ["cosine", "euclidean"]:
            raise ValueError(
                "`distance_metric` must be one of 'cosine' or 'euclidean'."
            )

        # Check embed model
        embed_function = metadata.get("embed_function", None)
        if embed_function is None:
            raise ValueError(
                "You must provide `embed_function` in metadata in order to "
                "use the default query function."
            )
        return partial(
            self.query_vector_collection,
            sources=metadata["sources"],
            chunk_strategy=chunk_strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            distance_metric=distance_metric,
            embed_function=embed_function,
        )

    @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
    def call_llm(self, prompt: str) -> str:
        """Call the LLM with the given prompt.

        Expects a function that takes a string and returns a string.

        Args:
            prompt (str): The prompt to send to the LLM.

        Returns:
            response (str): String representing the LLM response.
        """
        return self._llm_callable(prompt)

    def evaluate_with_llm(self, text: str, query_function: Callable) -> bool:
        """Validate that the LLM-generated text is supported by the provided
        contexts.

        Args:
            value (Any): The LLM-generated text.
            query_function (Callable): The query function.

        Returns:
            self_eval: The self-evaluation boolean
        """
        # Get the relevant chunks using the query function
        relevant_chunks = query_function(text=text, k=self._top_k)

        # Create the prompt to ask the LLM
        prompt = PROVENANCE_V1_PROMPT.format(text, "\n".join(relevant_chunks))

        # Get self-evaluation
        self_eval = self.call_llm(prompt)
        self_eval = True if self_eval == "Yes" else False
        return self_eval

    def validate_each_sentence(
        self, value: Any, query_function: Callable, metadata: Dict[str, Any]
    ) -> ValidationResult:
        if nltk is None:
            raise ImportError(
                "`nltk` library is required for `provenance-v0` validator. "
                "Please install it with `poetry add nltk`."
            )
        # Split the value into sentences using nltk sentence tokenizer.
        sentences = nltk.sent_tokenize(value)

        unsupported_sentences = []
        supported_sentences = []
        for sentence in sentences:
            self_eval = self.evaluate_with_llm(sentence, query_function)
            if not self_eval:
                unsupported_sentences.append(sentence)
            else:
                supported_sentences.append(sentence)

        if unsupported_sentences:
            unsupported_sentences = "- " + "\n- ".join(unsupported_sentences)
            return FailResult(
                metadata=metadata,
                error_message=(
                    f"None of the following sentences in your response are supported "
                    "by provided context:"
                    f"\n{unsupported_sentences}"
                ),
                fix_value="\n".join(supported_sentences),
            )
        return PassResult(metadata=metadata)

    def validate_full_text(
        self, value: Any, query_function: Callable, metadata: Dict[str, Any]
    ) -> ValidationResult:
        # Self-evaluate LLM with entire text
        self_eval = self.evaluate_with_llm(value, query_function)
        if not self_eval:
            # if false
            return FailResult(
                metadata=metadata,
                error_message=(
                    "The following text in your response is not supported by the "
                    "supported by the provided context:\n" + value
                ),
            )
        return PassResult(metadata=metadata)

    def validate(self, value: Any, metadata: Dict[str, Any]) -> ValidationResult:
        kwargs = {}
        context_copy = contextvars.copy_context()
        for key, context_var in context_copy.items():
            if key.name == "kwargs" and isinstance(kwargs, dict):
                kwargs = context_var
                break

        api_key = kwargs.get("api_key")
        api_base = kwargs.get("api_base")

        # Set the OpenAI API key
        if os.getenv("OPENAI_API_KEY"):  # Check if set in environment
            self.client.api_key = os.getenv("OPENAI_API_KEY")
        elif api_key:  # Check if set when calling guard() or parse()
            self.client.api_key = api_key

        # Set the OpenAI API base if specified
        if api_base:
            self.client.api_base = api_base

        query_function = self.get_query_function(metadata)
        if self._validation_method == "sentence":
            return self.validate_each_sentence(value, query_function, metadata)
        elif self._validation_method == "full":
            return self.validate_full_text(value, query_function, metadata)
        else:
            raise ValueError("validation_method must be 'sentence' or 'full'.")

    @staticmethod
    def query_vector_collection(
        text: str,
        k: int,
        sources: List[str],
        embed_function: Callable,
        chunk_strategy: str = "sentence",
        chunk_size: int = 5,
        chunk_overlap: int = 2,
        distance_metric: str = "cosine",
    ) -> List[Tuple[str, float]]:
        chunks = [
            get_chunks_from_text(source, chunk_strategy, chunk_size, chunk_overlap)
            for source in sources
        ]
        chunks = list(itertools.chain.from_iterable(chunks))

        # Create embeddings
        source_embeddings = np.array(embed_function(chunks)).squeeze()
        query_embedding = embed_function(text).squeeze()

        # Compute distances
        if distance_metric == "cosine":
            if not _HAS_NUMPY:
                raise ValueError(
                    "You must install numpy in order to use the cosine distance "
                    "metric."
                )

            cos_sim = 1 - (
                np.dot(source_embeddings, query_embedding)
                / (
                    np.linalg.norm(source_embeddings, axis=1)
                    * np.linalg.norm(query_embedding)
                )
            )
            top_indices = np.argsort(cos_sim)[:k]
            top_chunks = [chunks[j] for j in top_indices]
        else:
            raise ValueError("distance_metric must be 'cosine'.")

        return top_chunks


@register_validator(name="pii", data_type="string")
class PIIFilter(Validator):
    """Validates that any text does not contain any PII.

    This validator uses Microsoft's Presidio (https://github.com/microsoft/presidio)
    to detect PII in the text. If PII is detected, the validator will fail with a
    programmatic fix that anonymizes the text. Otherwise, the validator will pass.

    **Key Properties**

    | Property                      | Description                         |
    | ----------------------------- | ----------------------------------- |
    | Name for `format` attribute   | `pii`                               |
    | Supported data types          | `string`                            |
    | Programmatic fix              | Anonymized text with PII filtered   |

    Parameters: Arguments
        pii_entities (str | List[str], optional): The PII entities to filter. Must be
            one of `pii` or `spi`. Defaults to None. Can also be set in metadata.
    """

    PII_ENTITIES_MAP = {
        "pii": [
            "EMAIL_ADDRESS",
            "PHONE_NUMBER",
            "DOMAIN_NAME",
            "IP_ADDRESS",
            "DATE_TIME",
            "LOCATION",
            "PERSON",
            "URL",
        ],
        "spi": [
            "CREDIT_CARD",
            "CRYPTO",
            "IBAN_CODE",
            "NRP",
            "MEDICAL_LICENSE",
            "US_BANK_NUMBER",
            "US_DRIVER_LICENSE",
            "US_ITIN",
            "US_PASSPORT",
            "US_SSN",
        ],
    }

    def __init__(
        self,
        pii_entities: Union[str, List[str], None] = None,
        on_fail: Union[Callable[..., Any], None] = None,
        **kwargs,
    ):
        if AnalyzerEngine is None or AnonymizerEngine is None:
            raise ImportError(
                "You must install the `presidio-analyzer`, `presidio-anonymizer`"
                "and a spaCy language model to use the PII validator."
                "Refer to https://microsoft.github.io/presidio/installation/"
            )

        super().__init__(on_fail, pii_entities=pii_entities, **kwargs)
        self.pii_entities = pii_entities
        self.pii_analyzer = AnalyzerEngine()
        self.pii_anonymizer = AnonymizerEngine()

    def get_anonymized_text(self, text: str, entities: List[str]) -> str:
        """Analyze and anonymize the text for PII.

        Args:
            text (str): The text to analyze.
            pii_entities (List[str]): The PII entities to filter.

        Returns:
            anonymized_text (str): The anonymized text.
        """
        results = self.pii_analyzer.analyze(text=text, entities=entities, language="en")
        results = cast(List[Any], results)
        anonymized_text = self.pii_anonymizer.anonymize(
            text=text, analyzer_results=results
        ).text
        return anonymized_text

    def validate(self, value: Any, metadata: Dict[str, Any]) -> ValidationResult:
        # Entities to filter passed through metadata take precedence
        pii_entities = metadata.get("pii_entities", self.pii_entities)
        if pii_entities is None:
            raise ValueError(
                "`pii_entities` must be set in order to use the `PIIFilter` validator."
                "Add this: `pii_entities=['PERSON', 'PHONE_NUMBER']`"
                "OR pii_entities='pii' or 'spi'"
                "in init or metadata."
            )

        # Check that pii_entities is a string OR list of strings
        if isinstance(pii_entities, str):
            # A key to the PII_ENTITIES_MAP
            entities_to_filter = self.PII_ENTITIES_MAP.get(pii_entities, None)
            if entities_to_filter is None:
                raise ValueError(
                    f"`pii_entities` must be one of {self.PII_ENTITIES_MAP.keys()}"
                )
        elif isinstance(pii_entities, list):
            entities_to_filter = pii_entities
        else:
            raise ValueError(
                f"`pii_entities` must be one of {self.PII_ENTITIES_MAP.keys()}"
                " or a list of strings."
            )

        # Analyze the text, and anonymize it if there is PII
        anonymized_text = self.get_anonymized_text(
            text=value, entities=entities_to_filter
        )

        # If anonymized value text is different from original value, then there is PII
        if anonymized_text != value:
            return FailResult(
                error_message=(
                    f"The following text in your response contains PII:\n{value}"
                ),
                fix_value=anonymized_text,
            )
        return PassResult()


@register_validator(name="similar-to-list", data_type="string")
class SimilarToList(Validator):
    """Validates that a value is similar to a list of previously known values.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `similar-to-list`                 |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |

    Parameters: Arguments
        standard_deviations (int): The number of standard deviations from the mean to check.
        threshold (float): The threshold for the average semantic similarity for strings.

    For integer values, this validator checks whether the value lies
    within 'k' standard deviations of the mean of the previous values.
    (Assumes that the previous values are normally distributed.) For
    string values, this validator checks whether the average semantic
    similarity between the generated value and the previous values is
    less than a threshold.
    """  # noqa

    def __init__(
        self,
        standard_deviations: int = 3,
        threshold: float = 0.1,
        on_fail: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(
            on_fail,
            standard_deviations=standard_deviations,
            threshold=threshold,
            **kwargs,
        )
        self._standard_deviations = int(standard_deviations)
        self._threshold = float(threshold)

    def get_semantic_similarity(
        self, text1: str, text2: str, embed_function: Callable
    ) -> float:
        """Get the semantic similarity between two strings.

        Args:
            text1 (str): The first string.
            text2 (str): The second string.
            embed_function (Callable): The embedding function.
        Returns:
            similarity (float): The semantic similarity between the two strings.
        """
        text1_embedding = embed_function(text1)
        text2_embedding = embed_function(text2)
        similarity = 1 - (
            np.dot(text1_embedding, text2_embedding)
            / (np.linalg.norm(text1_embedding) * np.linalg.norm(text2_embedding))
        )
        return similarity

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        prev_values = metadata.get("prev_values", [])
        if not prev_values:
            raise ValueError("You must provide a list of previous values in metadata.")

        # Check if np is installed
        if not _HAS_NUMPY:
            raise ValueError(
                "You must install numpy in order to "
                "use the distribution check validator."
            )
        try:
            value = int(value)
            is_int = True
        except ValueError:
            is_int = False

        if is_int:
            # Check whether prev_values are also all integers
            if not all(isinstance(prev_value, int) for prev_value in prev_values):
                raise ValueError(
                    "Both given value and all the previous values must be "
                    "integers in order to use the distribution check validator."
                )

            # Check whether the value lies in a similar distribution as the prev_values
            # Get mean and std of prev_values
            prev_values = np.array(prev_values)
            prev_mean = np.mean(prev_values)  # type: ignore
            prev_std = np.std(prev_values)

            # Check whether the value lies outside specified stds of the mean
            if value < prev_mean - (
                self._standard_deviations * prev_std
            ) or value > prev_mean + (self._standard_deviations * prev_std):
                return FailResult(
                    error_message=(
                        f"The value {value} lies outside of the expected distribution "
                        f"of {prev_mean} +/- {self._standard_deviations * prev_std}."
                    ),
                )
            return PassResult()
        else:
            # Check whether prev_values are also all strings
            if not all(isinstance(prev_value, str) for prev_value in prev_values):
                raise ValueError(
                    "Both given value and all the previous values must be "
                    "strings in order to use the distribution check validator."
                )

            # Check embed model
            embed_function = metadata.get("embed_function", None)
            if embed_function is None:
                raise ValueError(
                    "You must provide `embed_function` in metadata in order to "
                    "check the semantic similarity of the generated string."
                )

            # Check whether the value is semantically similar to the prev_values
            # Get average semantic similarity
            # Lesser the average semantic similarity, more similar the strings are
            avg_semantic_similarity = np.mean(
                np.array(
                    [
                        self.get_semantic_similarity(value, prev_value, embed_function)
                        for prev_value in prev_values
                    ]
                )
            )

            # If average semantic similarity is above the threshold,
            # then the value is not semantically similar to the prev_values
            if avg_semantic_similarity > self._threshold:
                return FailResult(
                    error_message=(
                        f"The value {value} is not semantically similar to the "
                        f"previous values. The average semantic similarity is "
                        f"{avg_semantic_similarity} which is below the threshold of "
                        f"{self._threshold}."
                    ),
                )
            return PassResult()


@register_validator(name="detect-secrets", data_type="string")
class DetectSecrets(Validator):
    """Validates whether the generated code snippet contains any secrets.

    **Key Properties**
    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `detect-secrets`                  |
    | Supported data types          | `string`                          |
    | Programmatic fix              | None                              |

    Parameters: Arguments
        None

    This validator uses the detect-secrets library to check whether the generated code
    snippet contains any secrets. If any secrets are detected, the validator fails and
    returns the generated code snippet with the secrets replaced with asterisks.
    Else the validator returns the generated code snippet.

    Following are some caveats:
        - Multiple secrets on the same line may not be caught. e.g.
            - Minified code
            - One-line lists/dictionaries
            - Multi-variable assignments
        - Multi-line secrets may not be caught. e.g.
            - RSA/SSH keys

    Example:
        ```py

        guard = Guard.from_string(validators=[
            DetectSecrets(on_fail="fix")
        ])
        guard.parse(
            llm_output=code_snippet,
        )
    """

    def __init__(self, on_fail: Union[Callable[..., Any], None] = None, **kwargs):
        super().__init__(on_fail, **kwargs)

        # Check if detect-secrets is installed
        if detect_secrets is None:
            raise ValueError(
                "You must install detect-secrets in order to "
                "use the DetectSecrets validator."
            )
        self.temp_file_name = "temp.txt"
        self.mask = "********"

    def get_unique_secrets(self, value: str) -> Tuple[Dict[str, Any], List[str]]:
        """Get unique secrets from the value.

        Args:
            value (str): The generated code snippet.

        Returns:
            unique_secrets (Dict[str, Any]): A dictionary of unique secrets and their
                line numbers.
            lines (List[str]): The lines of the generated code snippet.
        """
        try:
            # Write each line of value to a new file
            with open(self.temp_file_name, "w") as f:
                f.writelines(value)
        except Exception as e:
            raise OSError(
                "Problems creating or deleting the temporary file. "
                "Please check the permissions of the current directory."
            ) from e

        try:
            # Create a new secrets collection
            from detect_secrets import settings
            from detect_secrets.core.secrets_collection import SecretsCollection

            secrets = SecretsCollection()

            # Scan the file for secrets
            with settings.default_settings():
                secrets.scan_file(self.temp_file_name)
        except ImportError:
            raise ValueError(
                "You must install detect-secrets in order to "
                "use the DetectSecrets validator."
            )
        except Exception as e:
            raise RuntimeError(
                "Problems with creating a SecretsCollection or "
                "scanning the file for secrets."
            ) from e

        # Get unique secrets from these secrets
        unique_secrets = {}
        for secret in secrets:
            _, potential_secret = secret
            actual_secret = potential_secret.secret_value
            line_number = potential_secret.line_number
            if actual_secret not in unique_secrets:
                unique_secrets[actual_secret] = [line_number]
            else:
                # if secret already exists, avoid duplicate line numbers
                if line_number not in unique_secrets[actual_secret]:
                    unique_secrets[actual_secret].append(line_number)

        try:
            # File no longer needed, read the lines from the file
            with open(self.temp_file_name, "r") as f:
                lines = f.readlines()
        except Exception as e:
            raise OSError(
                "Problems reading the temporary file. "
                "Please check the permissions of the current directory."
            ) from e

        try:
            # Delete the file
            os.remove(self.temp_file_name)
        except Exception as e:
            raise OSError(
                "Problems deleting the temporary file. "
                "Please check the permissions of the current directory."
            ) from e
        return unique_secrets, lines

    def get_modified_value(
        self, unique_secrets: Dict[str, Any], lines: List[str]
    ) -> str:
        """Replace the secrets on the lines with asterisks.

        Args:
            unique_secrets (Dict[str, Any]): A dictionary of unique secrets and their
                line numbers.
            lines (List[str]): The lines of the generated code snippet.

        Returns:
            modified_value (str): The generated code snippet with secrets replaced with
                asterisks.
        """
        # Replace the secrets on the lines with asterisks
        for secret, line_numbers in unique_secrets.items():
            for line_number in line_numbers:
                lines[line_number - 1] = lines[line_number - 1].replace(
                    secret, self.mask
                )

        # Convert lines to a multiline string
        modified_value = "".join(lines)
        return modified_value

    def validate(self, value: str, metadata: Dict[str, Any]) -> ValidationResult:
        # Check if value is a multiline string
        if "\n" not in value:
            # Raise warning if value is not a multiline string
            warnings.warn(
                "The DetectSecrets validator works best with "
                "multiline code snippets. "
                "Refer validator docs for more details."
            )

            # Add a newline to value
            value += "\n"

        # Get unique secrets from the value
        unique_secrets, lines = self.get_unique_secrets(value)

        if unique_secrets:
            # Replace the secrets on the lines with asterisks
            modified_value = self.get_modified_value(unique_secrets, lines)

            return FailResult(
                error_message=(
                    "The following secrets were detected in your response:\n"
                    + "\n".join(unique_secrets.keys())
                ),
                fix_value=modified_value,
            )
        return PassResult()


@register_validator(name="competitor-check", data_type="string")
class CompetitorCheck(Validator):
    """Validates that LLM-generated text is not naming any competitors from a
    given list.

    In order to use this validator you need to provide an extensive list of the
    competitors you want to avoid naming including all common variations.

    Args:
        competitors (List[str]): List of competitors you want to avoid naming
    """

    def __init__(
        self,
        competitors: List[str],
        on_fail: Optional[Callable] = None,
    ):
        super().__init__(competitors=competitors, on_fail=on_fail)
        self._competitors = competitors
        model = "en_core_web_trf"
        if spacy is None:
            raise ImportError(
                "You must install spacy in order to use the CompetitorCheck validator."
            )

        if not spacy.util.is_package(model):
            logger.info(
                f"Spacy model {model} not installed. "
                "Download should start now and take a few minutes."
            )
            spacy.cli.download(model)  # type: ignore

        self.nlp = spacy.load(model)

    def exact_match(self, text: str, competitors: List[str]) -> List[str]:
        """Performs exact match to find competitors from a list in a given
        text.

        Args:
            text (str): The text to search for competitors.
            competitors (list): A list of competitor entities to match.

        Returns:
            list: A list of matched entities.
        """

        found_entities = []
        for entity in competitors:
            pattern = rf"\b{re.escape(entity)}\b"
            match = re.search(pattern.lower(), text.lower())
            if match:
                found_entities.append(entity)
        return found_entities

    def perform_ner(self, text: str, nlp) -> List[str]:
        """Performs named entity recognition on text using a provided NLP
        model.

        Args:
            text (str): The text to perform named entity recognition on.
            nlp: The NLP model to use for entity recognition.

        Returns:
            entities: A list of entities found.
        """

        doc = nlp(text)
        entities = []
        for ent in doc.ents:
            entities.append(ent.text)
        return entities

    def is_entity_in_list(self, entities: List[str], competitors: List[str]) -> List:
        """Checks if any entity from a list is present in a given list of
        competitors.

        Args:
            entities (list): A list of entities to check
            competitors (list): A list of competitor names to match

        Returns:
            List: List of found competitors
        """

        found_competitors = []
        for entity in entities:
            for item in competitors:
                pattern = rf"\b{re.escape(item)}\b"
                match = re.search(pattern.lower(), entity.lower())
                if match:
                    found_competitors.append(item)
        return found_competitors

    def validate(self, value: str, metadata=Dict) -> ValidationResult:
        """Checks a text to find competitors' names in it.

        While running, store sentences naming competitors and generate a fixed output
        filtering out all flagged sentences.

        Args:
            value (str): The value to be validated.
            metadata (Dict, optional): Additional metadata. Defaults to empty dict.

        Returns:
            ValidationResult: The validation result.
        """

        if nltk is None:
            raise ImportError(
                "`nltk` library is required for `competitors-check` validator. "
                "Please install it with `poetry add nltk`."
            )
        sentences = nltk.sent_tokenize(value)
        flagged_sentences = []
        filtered_sentences = []
        list_of_competitors_found = []

        for sentence in sentences:
            entities = self.exact_match(sentence, self._competitors)
            if entities:
                ner_entities = self.perform_ner(sentence, self.nlp)
                found_competitors = self.is_entity_in_list(ner_entities, entities)

                if found_competitors:
                    flagged_sentences.append((found_competitors, sentence))
                    list_of_competitors_found.append(found_competitors)
                    logger.debug(f"Found: {found_competitors} named in '{sentence}'")
                else:
                    filtered_sentences.append(sentence)

            else:
                filtered_sentences.append(sentence)

        filtered_output = " ".join(filtered_sentences)

        if len(flagged_sentences):
            return FailResult(
                error_message=(
                    f"Found the following competitors: {list_of_competitors_found}. "
                    "Please avoid naming those competitors next time"
                ),
                fix_value=filtered_output,
            )
        else:
            return PassResult()
