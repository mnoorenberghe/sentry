from __future__ import annotations

from datetime import datetime
from functools import reduce
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union

from sentry_relay.consts import SPAN_STATUS_NAME_TO_CODE
from sentry_relay.processing import parse_release as parse_release_relay

from sentry.api.event_search import (
    AggregateFilter,
    ParenExpression,
    SearchBoolean,
    SearchFilter,
    SearchKey,
    SearchValue,
)
from sentry.api.release_search import INVALID_SEMVER_MESSAGE
from sentry.constants import SEMVER_FAKE_PACKAGE
from sentry.exceptions import InvalidSearchQuery
from sentry.models.group import Group
from sentry.models.project import Project
from sentry.models.release import Release, SemverFilter
from sentry.search.events.constants import (
    ARRAY_FIELDS,
    EQUALITY_OPERATORS,
    ERROR_UNHANDLED_ALIAS,
    ISSUE_ALIAS,
    ISSUE_ID_ALIAS,
    MAX_SEARCH_RELEASES,
    NO_CONVERSION_FIELDS,
    OPERATOR_NEGATION_MAP,
    OPERATOR_TO_DJANGO,
    PROJECT_ALIAS,
    PROJECT_NAME_ALIAS,
    RELEASE_ALIAS,
    RELEASE_STAGE_ALIAS,
    SEMVER_ALIAS,
    SEMVER_BUILD_ALIAS,
    SEMVER_EMPTY_RELEASE,
    SEMVER_PACKAGE_ALIAS,
    SEMVER_WILDCARDS,
    TEAM_KEY_TRANSACTION_ALIAS,
    TRANSACTION_STATUS_ALIAS,
    USER_DISPLAY_ALIAS,
)
from sentry.search.events.fields import FIELD_ALIASES, FUNCTIONS, resolve_field
from sentry.search.utils import parse_release
from sentry.utils.dates import to_timestamp
from sentry.utils.snuba import FUNCTION_TO_OPERATOR, OPERATOR_TO_FUNCTION, SNUBA_AND, SNUBA_OR
from sentry.utils.strings import oxfordize_list
from sentry.utils.validators import INVALID_ID_DETAILS, INVALID_SPAN_ID, WILDCARD_NOT_ALLOWED


def is_condition(term):
    return isinstance(term, (tuple, list)) and len(term) == 3 and term[1] in OPERATOR_TO_FUNCTION


def translate_transaction_status(val: str) -> str:
    if val not in SPAN_STATUS_NAME_TO_CODE:
        raise InvalidSearchQuery(
            f"Invalid value {val} for transaction.status condition. Accepted "
            f"values are {oxfordize_list([str(key) for key in SPAN_STATUS_NAME_TO_CODE.keys()])}"
        )
    return SPAN_STATUS_NAME_TO_CODE[val]


def to_list(value: Union[List[str], str]) -> List[str]:
    if isinstance(value, list):
        return value
    return [value]


def convert_condition_to_function(cond):
    if len(cond) != 3:
        return cond
    function = OPERATOR_TO_FUNCTION.get(cond[1])
    if not function:
        # It's hard to make this error more specific without exposing internals to the end user
        raise InvalidSearchQuery(f"Operator {cond[1]} is not a valid condition operator.")

    return [function, [cond[0], cond[2]]]


def convert_array_to_tree(operator, terms):
    """
    Convert an array of conditions into a binary tree joined by the operator.
    """
    if len(terms) == 1:
        return terms[0]
    elif len(terms) == 2:
        return [operator, terms]
    elif terms[1] in ["IN", "NOT IN"]:
        return terms

    return [operator, [terms[0], convert_array_to_tree(operator, terms[1:])]]


def convert_aggregate_filter_to_snuba_query(aggregate_filter, params):
    name = aggregate_filter.key.name
    value = aggregate_filter.value.value

    if params is not None and name in params.get("aliases", {}):
        return params["aliases"][name].converter(aggregate_filter)

    value = (
        int(to_timestamp(value)) if isinstance(value, datetime) and name != "timestamp" else value
    )

    if aggregate_filter.operator in ("=", "!=") and aggregate_filter.value.value == "":
        return [["isNull", [name]], aggregate_filter.operator, 1]

    function = resolve_field(name, params, functions_acl=FUNCTIONS.keys())
    if function.aggregate is not None:
        name = function.aggregate[-1]

    return [name, aggregate_filter.operator, value]


def convert_function_to_condition(func):
    if len(func) != 2:
        return func
    operator = FUNCTION_TO_OPERATOR.get(func[0])
    if not operator:
        return [func, "=", 1]

    return [func[1][0], operator, func[1][1]]


def _environment_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    # conditions added to env_conditions are OR'd
    env_conditions = []
    value = search_filter.value.value
    values = set(value if isinstance(value, (list, tuple)) else [value])
    # the "no environment" environment is null in snuba
    if "" in values:
        values.remove("")
        operator = "IS NULL" if search_filter.operator == "=" else "IS NOT NULL"
        env_conditions.append(["environment", operator, None])
    if len(values) == 1:
        operator = "=" if search_filter.operator in EQUALITY_OPERATORS else "!="
        env_conditions.append(["environment", operator, values.pop()])
    elif values:
        operator = "IN" if search_filter.operator in EQUALITY_OPERATORS else "NOT IN"
        env_conditions.append(["environment", operator, values])
    return env_conditions


def _message_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    value = search_filter.value.value
    if search_filter.value.is_wildcard():
        # XXX: We don't want the '^$' values at the beginning and end of
        # the regex since we want to find the pattern anywhere in the
        # message. Strip off here
        value = search_filter.value.value[1:-1]
        return [["match", ["message", f"'(?i){value}'"]], search_filter.operator, 1]
    elif value == "":
        operator = "=" if search_filter.operator == "=" else "!="
        return [["equals", ["message", f"{value}"]], operator, 1]
    else:
        # https://clickhouse.yandex/docs/en/query_language/functions/string_search_functions/#position-haystack-needle
        # positionCaseInsensitive returns 0 if not found and an index of 1 or more if found
        # so we should flip the operator here
        operator = "!=" if search_filter.operator in EQUALITY_OPERATORS else "="
        if search_filter.is_in_filter:
            # XXX: This `toString` usage is unnecessary, but we need it in place to
            # trick the legacy Snuba language into not treating `message` as a
            # function. Once we switch over to snql it can be removed.
            return [
                [
                    "multiSearchFirstPositionCaseInsensitive",
                    [["toString", ["message"]], ["array", [f"'{v}'" for v in value]]],
                ],
                operator,
                0,
            ]

        # make message search case insensitive
        return [["positionCaseInsensitive", ["message", f"'{value}'"]], operator, 0]


def _transaction_status_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    # Handle "has" queries
    if search_filter.value.raw_value == "":
        return [["isNull", [name]], search_filter.operator, 1]

    if search_filter.is_in_filter:
        internal_value = [
            translate_transaction_status(val) for val in search_filter.value.raw_value
        ]
    else:
        internal_value = translate_transaction_status(search_filter.value.raw_value)

    return [name, search_filter.operator, internal_value]


def _issue_id_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    value = search_filter.value.value
    # Handle "has" queries
    if (
        search_filter.value.raw_value == ""
        or search_filter.is_in_filter
        and [v for v in value if not v]
    ):
        # The state of having no issues is represented differently on transactions vs
        # other events. On the transactions table, it is represented by 0 whereas it is
        # represented by NULL everywhere else. We use coalesce here so we can treat this
        # consistently
        name = ["coalesce", [name, 0]]
        if search_filter.is_in_filter:
            value = [v if v else 0 for v in value]
        else:
            value = 0

    # Skip isNull check on group_id value as we want to
    # allow snuba's prewhere optimizer to find this condition.
    return [name, search_filter.operator, value]


def _user_display_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    value = search_filter.value.value
    user_display_expr = FIELD_ALIASES[USER_DISPLAY_ALIAS].get_expression(params)

    # Handle 'has' condition
    if search_filter.value.raw_value == "":
        return [["isNull", [user_display_expr]], search_filter.operator, 1]
    if search_filter.value.is_wildcard():
        return [
            ["match", [user_display_expr, f"'(?i){value}'"]],
            search_filter.operator,
            1,
        ]
    return [user_display_expr, search_filter.operator, value]


def _error_unhandled_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    value = search_filter.value.value
    # This field is the inversion of error.handled, otherwise the logic is the same.
    if search_filter.value.raw_value == "":
        output = 0 if search_filter.operator == "!=" else 1
        return [["isHandled", []], "=", output]
    if value in ("1", 1):
        return [["notHandled", []], "=", 1]
    if value in ("0", 0):
        return [["isHandled", []], "=", 1]
    raise InvalidSearchQuery(
        "Invalid value for error.unhandled condition. Accepted values are 1, 0"
    )


def _error_handled_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    value = search_filter.value.value
    # Treat has filter as equivalent to handled
    if search_filter.value.raw_value == "":
        output = 1 if search_filter.operator == "!=" else 0
        return [["isHandled", []], "=", output]
    # Null values and 1 are the same, and both indicate a handled error.
    if value in ("1", 1):
        return [["isHandled", []], "=", 1]
    if value in ("0", 0):
        return [["notHandled", []], "=", 1]
    raise InvalidSearchQuery("Invalid value for error.handled condition. Accepted values are 1, 0")


def _team_key_transaction_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
):
    value = search_filter.value.value
    key_transaction_expr = FIELD_ALIASES[TEAM_KEY_TRANSACTION_ALIAS].get_field(params)

    if search_filter.value.raw_value == "":
        operator = "!=" if search_filter.operator == "!=" else "="
        return [key_transaction_expr, operator, 0]
    if value in ("1", 1):
        return [key_transaction_expr, "=", 1]
    if value in ("0", 0):
        return [key_transaction_expr, "=", 0]
    raise InvalidSearchQuery(
        "Invalid value for team_key_transaction condition. Accepted values are 1, 0"
    )


def _flip_field_sort(field: str):
    return field[1:] if field.startswith("-") else f"-{field}"


def _release_stage_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
) -> Tuple[str, str, Sequence[str]]:
    """
    Parses a release stage search and returns a snuba condition to filter to the
    requested releases.
    """
    # TODO: Filter by project here as well. It's done elsewhere, but could critically limit versions
    # for orgs with thousands of projects, each with their own releases (potentially drowning out ones we care about)

    if not params or "organization_id" not in params:
        raise ValueError("organization_id is a required param")

    organization_id: int = params["organization_id"]
    project_ids: Optional[list[int]] = params.get("project_id")
    environments: Optional[list[int]] = params.get("environment")
    qs = (
        Release.objects.filter_by_stage(
            organization_id,
            search_filter.operator,
            search_filter.value.value,
            project_ids=project_ids,
            environments=environments,
        )
        .values_list("version", flat=True)
        .order_by("date_added")[:MAX_SEARCH_RELEASES]
    )
    versions = list(qs)
    final_operator = "IN"

    if not versions:
        # XXX: Just return a filter that will return no results if we have no versions
        versions = [SEMVER_EMPTY_RELEASE]

    return ["release", final_operator, versions]


def _semver_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
) -> Tuple[str, str, Sequence[str]]:
    """
    Parses a semver query search and returns a snuba condition to filter to the
    requested releases.

    Since we only have semver information available in Postgres currently, we query
    Postgres and return a list of versions to include/exclude. For most customers this
    will work well, however some have extremely large numbers of releases, and we can't
    pass them all to Snuba. To try and serve reasonable results, we:
     - Attempt to query based on the initial semver query. If this returns
       MAX_SEMVER_SEARCH_RELEASES results, we invert the query and see if it returns
       fewer results. If so, we use a `NOT IN` snuba condition instead of an `IN`.
     - Order the results such that the versions we return are semantically closest to
       the passed filter. This means that when searching for `>= 1.0.0`, we'll return
       version 1.0.0, 1.0.1, 1.1.0 before 9.x.x.
    """
    if not params or "organization_id" not in params:
        raise ValueError("organization_id is a required param")

    organization_id: int = params["organization_id"]
    project_ids: Optional[list[int]] = params.get("project_id")
    # We explicitly use `raw_value` here to avoid converting wildcards to shell values
    version: str = search_filter.value.raw_value
    operator: str = search_filter.operator

    # Note that we sort this such that if we end up fetching more than
    # MAX_SEMVER_SEARCH_RELEASES, we will return the releases that are closest to
    # the passed filter.
    order_by = Release.SEMVER_COLS
    if operator.startswith("<"):
        order_by = list(map(_flip_field_sort, order_by))
    qs = (
        Release.objects.filter_by_semver(
            organization_id,
            parse_semver(version, operator),
            project_ids=project_ids,
        )
        .values_list("version", flat=True)
        .order_by(*order_by)[:MAX_SEARCH_RELEASES]
    )
    versions = list(qs)
    final_operator = "IN"
    if len(versions) == MAX_SEARCH_RELEASES:
        # We want to limit how many versions we pass through to Snuba. If we've hit
        # the limit, make an extra query and see whether the inverse has fewer ids.
        # If so, we can do a NOT IN query with these ids instead. Otherwise, we just
        # do our best.
        operator = OPERATOR_NEGATION_MAP[operator]
        # Note that the `order_by` here is important for index usage. Postgres seems
        # to seq scan with this query if the `order_by` isn't included, so we
        # include it even though we don't really care about order for this query
        qs_flipped = (
            Release.objects.filter_by_semver(organization_id, parse_semver(version, operator))
            .order_by(*map(_flip_field_sort, order_by))
            .values_list("version", flat=True)[:MAX_SEARCH_RELEASES]
        )

        exclude_versions = list(qs_flipped)
        if exclude_versions and len(exclude_versions) < len(versions):
            # Do a negative search instead
            final_operator = "NOT IN"
            versions = exclude_versions

    if not versions:
        # XXX: Just return a filter that will return no results if we have no versions
        versions = [SEMVER_EMPTY_RELEASE]

    return ["release", final_operator, versions]


def _semver_package_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
) -> Tuple[str, str, Sequence[str]]:
    """
    Applies a semver package filter to the search. Note that if the query returns more than
    `MAX_SEARCH_RELEASES` here we arbitrarily return a subset of the releases.
    """
    if not params or "organization_id" not in params:
        raise ValueError("organization_id is a required param")

    organization_id: int = params["organization_id"]
    project_ids: Optional[list[int]] = params.get("project_id")
    package: str = search_filter.value.raw_value

    versions = list(
        Release.objects.filter_by_semver(
            organization_id,
            SemverFilter("exact", [], package),
            project_ids=project_ids,
        ).values_list("version", flat=True)[:MAX_SEARCH_RELEASES]
    )

    if not versions:
        # XXX: Just return a filter that will return no results if we have no versions
        versions = [SEMVER_EMPTY_RELEASE]

    return ["release", "IN", versions]


def _semver_build_filter_converter(
    search_filter: SearchFilter,
    name: str,
    params: Optional[Mapping[str, Union[int, str, datetime]]],
) -> Tuple[str, str, Sequence[str]]:
    """
    Applies a semver build filter to the search. Note that if the query returns more than
    `MAX_SEARCH_RELEASES` here we arbitrarily return a subset of the releases.
    """
    if not params or "organization_id" not in params:
        raise ValueError("organization_id is a required param")

    organization_id: int = params["organization_id"]
    project_ids: Optional[list[int]] = params.get("project_id")
    build: str = search_filter.value.raw_value

    operator, negated = handle_operator_negation(search_filter.operator)
    try:
        django_op = OPERATOR_TO_DJANGO[operator]
    except KeyError:
        raise InvalidSearchQuery("Invalid operation 'IN' for semantic version filter.")

    versions = list(
        Release.objects.filter_by_semver_build(
            organization_id,
            django_op,
            build,
            project_ids=project_ids,
            negated=negated,
        ).values_list("version", flat=True)[:MAX_SEARCH_RELEASES]
    )

    if not versions:
        # XXX: Just return a filter that will return no results if we have no versions
        versions = [SEMVER_EMPTY_RELEASE]

    return ["release", "IN", versions]


def handle_operator_negation(operator: str) -> Tuple[str, bool]:
    negated = False
    if operator == "!=":
        negated = True
        operator = "="
    return operator, negated


def parse_semver(version, operator) -> SemverFilter:
    """
    Attempts to parse a release version using our semver syntax. version should be in
    format `<package_name>@<version>` or `<version>`, where package_name is a string and
    version is a version string matching semver format (https://semver.org/). We've
    slightly extended this format to allow up to 4 integers. EG
     - sentry@1.2.3.4
     - sentry@1.2.3.4-alpha
     - 1.2.3.4
     - 1.2.3.4-alpha
     - 1.*
    """
    (operator, negated) = handle_operator_negation(operator)
    try:
        operator = OPERATOR_TO_DJANGO[operator]
    except KeyError:
        raise InvalidSearchQuery("Invalid operation 'IN' for semantic version filter.")

    version = version if "@" in version else f"{SEMVER_FAKE_PACKAGE}@{version}"
    parsed = parse_release_relay(version)
    parsed_version = parsed.get("version_parsed")
    if parsed_version:
        # Convert `pre` to always be a string
        prerelease = parsed_version["pre"] if parsed_version["pre"] else ""
        semver_filter = SemverFilter(
            operator,
            [
                parsed_version["major"],
                parsed_version["minor"],
                parsed_version["patch"],
                parsed_version["revision"],
                0 if prerelease else 1,
                prerelease,
            ],
            negated=negated,
        )
        if parsed["package"] and parsed["package"] != SEMVER_FAKE_PACKAGE:
            semver_filter.package = parsed["package"]
        return semver_filter
    else:
        # Try to parse as a wildcard match
        package, version = version.split("@", 1)
        version_parts = []
        if version:
            for part in version.split(".", 3):
                if part in SEMVER_WILDCARDS:
                    break
                try:
                    # We assume all ints for a wildcard match - not handling prerelease as
                    # part of these
                    version_parts.append(int(part))
                except ValueError:
                    raise InvalidSearchQuery(INVALID_SEMVER_MESSAGE)

        package = package if package and package != SEMVER_FAKE_PACKAGE else None
        return SemverFilter("exact", version_parts, package, negated)


key_conversion_map: Mapping[
    str,
    Callable[[SearchFilter, str, Mapping[str, Union[int, str, datetime]]], Optional[Sequence[Any]]],
] = {
    "environment": _environment_filter_converter,
    "message": _message_filter_converter,
    TRANSACTION_STATUS_ALIAS: _transaction_status_filter_converter,
    "issue.id": _issue_id_filter_converter,
    USER_DISPLAY_ALIAS: _user_display_filter_converter,
    ERROR_UNHANDLED_ALIAS: _error_unhandled_filter_converter,
    "error.handled": _error_handled_filter_converter,
    TEAM_KEY_TRANSACTION_ALIAS: _team_key_transaction_filter_converter,
    RELEASE_STAGE_ALIAS: _release_stage_filter_converter,
    SEMVER_ALIAS: _semver_filter_converter,
    SEMVER_PACKAGE_ALIAS: _semver_package_filter_converter,
    SEMVER_BUILD_ALIAS: _semver_build_filter_converter,
}


def convert_search_filter_to_snuba_query(
    search_filter: SearchFilter,
    key: Optional[str] = None,
    params: Optional[Mapping[str, Union[int, str, datetime]]] = None,
) -> Optional[Sequence[Any]]:
    name = search_filter.key.name if key is None else key
    value = search_filter.value.value

    # We want to use group_id elsewhere so shouldn't be removed from the dataset
    # but if a user has a tag with the same name we want to make sure that works
    if name in {"group_id"}:
        name = f"tags[{name}]"

    if name in NO_CONVERSION_FIELDS:
        return None
    elif name in key_conversion_map:
        return key_conversion_map[name](search_filter, name, params)
    elif name in ARRAY_FIELDS and search_filter.value.is_wildcard():
        # Escape and convert meta characters for LIKE expressions.
        raw_value = search_filter.value.raw_value
        # TODO: There are rare cases where this chaining don't
        # work. For example, a wildcard like '\**' will incorrectly
        # be replaced with '\%%'.
        like_value = (
            # Slashes have to be double escaped so they are
            # interpreted as a string literal.
            raw_value.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            .replace("*", "%")
        )
        operator = "LIKE" if search_filter.operator == "=" else "NOT LIKE"
        return [name, operator, like_value]
    elif name in ARRAY_FIELDS and search_filter.is_in_filter:
        operator = "=" if search_filter.operator == "IN" else "!="
        # XXX: This `arrayConcat` usage is unnecessary, but we need it in place to
        # trick the legacy Snuba language into not treating `name` as a
        # function. Once we switch over to snql it can be removed.
        return [
            ["hasAny", [["arrayConcat", [name]], ["array", [f"'{v}'" for v in value]]]],
            operator,
            1,
        ]
    elif name in ARRAY_FIELDS and search_filter.value.raw_value == "":
        return [["notEmpty", [name]], "=", 1 if search_filter.operator == "!=" else 0]
    else:
        # timestamp{,.to_{hour,day}} need a datetime string
        # last_seen needs an integer
        if isinstance(value, datetime) and name not in {
            "timestamp",
            "timestamp.to_hour",
            "timestamp.to_day",
        }:
            value = int(to_timestamp(value)) * 1000

        if name in {"trace.span", "trace.parent_span"}:
            if search_filter.value.is_wildcard():
                raise InvalidSearchQuery(WILDCARD_NOT_ALLOWED.format(name))
            if not search_filter.value.is_span_id():
                raise InvalidSearchQuery(INVALID_SPAN_ID.format(name))

        # Validate event ids and trace ids are uuids
        if name in {"id", "trace"}:
            if search_filter.value.is_wildcard():
                raise InvalidSearchQuery(WILDCARD_NOT_ALLOWED.format(name))
            elif not search_filter.value.is_event_id():
                label = "Filter ID" if name == "id" else "Filter Trace ID"
                raise InvalidSearchQuery(INVALID_ID_DETAILS.format(label))

        # most field aliases are handled above but timestamp.to_{hour,day} are
        # handled here
        if name in FIELD_ALIASES:
            name = FIELD_ALIASES[name].get_field(params)

        # Tags are never null, but promoted tags are columns and so can be null.
        # To handle both cases, use `ifNull` to convert to an empty string and
        # compare so we need to check for empty values.
        if search_filter.key.is_tag:
            name = ["ifNull", [name, "''"]]

        # Handle checks for existence
        if search_filter.operator in ("=", "!=") and search_filter.value.value == "":
            if search_filter.key.is_tag:
                return [name, search_filter.operator, value]
            else:
                # If not a tag, we can just check that the column is null.
                return [["isNull", [name]], search_filter.operator, 1]

        is_null_condition = None
        # TODO(wmak): Skip this for all non-nullable keys not just event.type
        if (
            search_filter.operator in ("!=", "NOT IN")
            and not search_filter.key.is_tag
            and name != "event.type"
        ):
            # Handle null columns on inequality comparisons. Any comparison
            # between a value and a null will result to null, so we need to
            # explicitly check for whether the condition is null, and OR it
            # together with the inequality check.
            # We don't need to apply this for tags, since if they don't exist
            # they'll always be an empty string.
            is_null_condition = [["isNull", [name]], "=", 1]

        if search_filter.value.is_wildcard():
            condition = [["match", [name, f"'(?i){value}'"]], search_filter.operator, 1]
        else:
            condition = [name, search_filter.operator, value]

        # We only want to return as a list if we have the check for null
        # present. Returning as a list causes these conditions to be ORed
        # together. Otherwise just return the raw condition, so that it can be
        # used correctly in aggregates.
        if is_null_condition:
            return [is_null_condition, condition]
        else:
            return condition


def flatten_condition_tree(tree, condition_function):
    """
    Take a binary tree of conditions, and flatten all of the terms using the condition function.
    E.g. f( and(and(b, c), and(d, e)), and ) -> [b, c, d, e]
    """
    stack = [tree]
    flattened = []
    while len(stack) > 0:
        item = stack.pop(0)
        if item[0] == condition_function:
            stack.extend(item[1])
        else:
            flattened.append(item)

    return flattened


def convert_snuba_condition_to_function(term, params=None):
    if isinstance(term, ParenExpression):
        return convert_search_boolean_to_snuba_query(term.children, params)

    group_ids = []
    projects_to_filter = []
    if isinstance(term, SearchFilter):
        conditions, projects_to_filter, group_ids = format_search_filter(term, params)
        group_ids = group_ids if group_ids else []
        if conditions:
            conditions_to_and = []
            for cond in conditions:
                if is_condition(cond):
                    conditions_to_and.append(convert_condition_to_function(cond))
                else:
                    conditions_to_and.append(
                        convert_array_to_tree(
                            SNUBA_OR, [convert_condition_to_function(c) for c in cond]
                        )
                    )

            condition_tree = None
            if len(conditions_to_and) == 1:
                condition_tree = conditions_to_and[0]
            elif len(conditions_to_and) > 1:
                condition_tree = convert_array_to_tree(SNUBA_AND, conditions_to_and)
            return condition_tree, None, projects_to_filter, group_ids
    elif isinstance(term, AggregateFilter):
        converted_filter = convert_aggregate_filter_to_snuba_query(term, params)
        return None, convert_condition_to_function(converted_filter), projects_to_filter, group_ids

    return None, None, projects_to_filter, group_ids


def convert_search_boolean_to_snuba_query(terms, params=None):
    if len(terms) == 1:
        return convert_snuba_condition_to_function(terms[0], params)

    # Filter out any ANDs since we can assume anything without an OR is an AND. Also do some
    # basic sanitization of the query: can't have two operators next to each other, and can't
    # start or end a query with an operator.
    prev = None
    new_terms = []
    term = None

    for term in terms:
        if prev:
            if SearchBoolean.is_operator(prev) and SearchBoolean.is_operator(term):
                raise InvalidSearchQuery(
                    f"Missing condition in between two condition operators: '{prev} {term}'"
                )
        else:
            if SearchBoolean.is_operator(term):
                raise InvalidSearchQuery(
                    f"Condition is missing on the left side of '{term}' operator"
                )

        if term != SearchBoolean.BOOLEAN_AND:
            new_terms.append(term)
        prev = term
    if term is not None and SearchBoolean.is_operator(term):
        raise InvalidSearchQuery(f"Condition is missing on the right side of '{term}' operator")
    terms = new_terms

    # We put precedence on AND, which sort of counter-intuitively means we have to split the query
    # on ORs first, so the ANDs are grouped together. Search through the query for ORs and split the
    # query on each OR.
    # We want to maintain a binary tree, so split the terms on the first OR we can find and recurse on
    # the two sides. If there is no OR, split the first element out to AND
    index = None
    lhs, rhs = None, None
    operator = None
    try:
        index = terms.index(SearchBoolean.BOOLEAN_OR)
        lhs, rhs = terms[:index], terms[index + 1 :]
        operator = SNUBA_OR
    except Exception:
        lhs, rhs = terms[:1], terms[1:]
        operator = SNUBA_AND

    (
        lhs_condition,
        lhs_having,
        projects_to_filter,
        group_ids,
    ) = convert_search_boolean_to_snuba_query(lhs, params)
    (
        rhs_condition,
        rhs_having,
        rhs_projects_to_filter,
        rhs_group_ids,
    ) = convert_search_boolean_to_snuba_query(rhs, params)

    projects_to_filter.extend(rhs_projects_to_filter)
    group_ids.extend(rhs_group_ids)

    if operator == SNUBA_OR and (lhs_condition or rhs_condition) and (lhs_having or rhs_having):
        raise InvalidSearchQuery(
            "Having an OR between aggregate filters and normal filters is invalid."
        )

    condition, having = None, None
    if lhs_condition or rhs_condition:
        args = list(filter(None, [lhs_condition, rhs_condition]))
        if not args:
            condition = None
        elif len(args) == 1:
            condition = args[0]
        else:
            condition = [operator, args]

    if lhs_having or rhs_having:
        args = list(filter(None, [lhs_having, rhs_having]))
        if not args:
            having = None
        elif len(args) == 1:
            having = args[0]
        else:
            having = [operator, args]

    return condition, having, projects_to_filter, group_ids


def format_search_filter(term, params):
    projects_to_filter = []  # Used to avoid doing multiple conditions on project ID
    conditions = []
    group_ids = None
    name = term.key.name
    value = term.value.value
    if name in (PROJECT_ALIAS, PROJECT_NAME_ALIAS):
        if term.operator == "=" and value == "":
            raise InvalidSearchQuery("Invalid query for 'has' search: 'project' cannot be empty.")
        slugs = to_list(value)
        projects = {
            p.slug: p.id
            for p in Project.objects.filter(id__in=params.get("project_id", []), slug__in=slugs)
        }
        missing = [slug for slug in slugs if slug not in projects]
        if missing:
            if term.operator in EQUALITY_OPERATORS:
                raise InvalidSearchQuery(
                    f"Invalid query. Project(s) {oxfordize_list(missing)} do not exist or are not actively selected."
                )

        project_ids = list(sorted(projects.values()))
        if project_ids:
            # Create a new search filter with the correct values
            term = SearchFilter(
                SearchKey("project_id"),
                term.operator,
                SearchValue(project_ids if term.is_in_filter else project_ids[0]),
            )
            converted_filter = convert_search_filter_to_snuba_query(term)
            if converted_filter:
                if term.operator in EQUALITY_OPERATORS:
                    projects_to_filter = project_ids
                conditions.append(converted_filter)
    elif name == ISSUE_ID_ALIAS and value != "":
        # A blank term value means that this is a 'has' filter
        if term.operator in EQUALITY_OPERATORS:
            group_ids = to_list(value)
        else:
            converted_filter = convert_search_filter_to_snuba_query(term, params=params)
            if converted_filter:
                conditions.append(converted_filter)
    elif name == ISSUE_ALIAS:
        operator = term.operator
        value = to_list(value)
        # `unknown` is a special value for when there is no issue associated with the event
        group_short_ids = [v for v in value if v and v != "unknown"]
        filter_values = ["" for v in value if not v or v == "unknown"]

        if group_short_ids and params and "organization_id" in params:
            try:
                groups = Group.objects.by_qualified_short_id_bulk(
                    params["organization_id"],
                    group_short_ids,
                )
            except Exception:
                raise InvalidSearchQuery(f"Invalid value '{group_short_ids}' for 'issue:' filter")
            else:
                filter_values.extend(sorted(g.id for g in groups))

        term = SearchFilter(
            SearchKey("issue.id"),
            operator,
            SearchValue(filter_values if term.is_in_filter else filter_values[0]),
        )
        converted_filter = convert_search_filter_to_snuba_query(term)
        conditions.append(converted_filter)
    elif (
        name == RELEASE_ALIAS
        and params
        and (value == "latest" or term.is_in_filter and any(v == "latest" for v in value))
    ):
        value = reduce(
            lambda x, y: x + y,
            [
                parse_release(
                    v,
                    params["project_id"],
                    params.get("environment_objects"),
                    params.get("organization_id"),
                )
                for v in to_list(value)
            ],
            [],
        )

        operator_conversions = {"=": "IN", "!=": "NOT IN"}
        operator = operator_conversions.get(term.operator, term.operator)

        converted_filter = convert_search_filter_to_snuba_query(
            SearchFilter(
                term.key,
                operator,
                SearchValue(value),
            )
        )
        if converted_filter:
            conditions.append(converted_filter)
    else:
        converted_filter = convert_search_filter_to_snuba_query(term, params=params)
        if converted_filter:
            conditions.append(converted_filter)

    return conditions, projects_to_filter, group_ids


# Not a part of search.events.types to avoid a circular loop
ParsedTerm = Union[SearchFilter, AggregateFilter]
ParsedTerms = Sequence[ParsedTerm]
