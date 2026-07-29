"""Expression evaluator: capability and, more importantly, containment."""

from __future__ import annotations

import pytest

from app.dsl.errors import DslError
from app.dsl.expressions import (
    EvalContext,
    referenced_params,
    render,
)


@pytest.fixture
def ctx():
    return EvalContext(
        para={"n": 3, "line": "A", "flag": True},
        case={"name": "C1"},
        role={"pm": "alice"},
    )


class TestEvaluation:
    @pytest.mark.parametrize(
        "source,expected",
        [
            ("{{ 1 + 2 }}", 3),
            ("{{ para.n * 12 }}", 36),
            ("{{ para.n - 1 }}", 2),
            ("{{ para.n / 2 }}", 1.5),
            ("{{ para.n // 2 }}", 1),
            ("{{ para.n % 2 }}", 1),
            ("{{ -para.n }}", -3),
            ("{{ para.line == 'A' }}", True),
            ("{{ para.line != 'A' }}", False),
            ("{{ para.n > 1 and para.flag }}", True),
            ("{{ para.n > 5 or para.flag }}", True),
            ("{{ not para.flag }}", False),
            ("{{ para.line in ['A', 'B'] }}", True),
            ("{{ para.line not in ['B'] }}", True),
            ("{{ 1 < para.n <= 3 }}", True),
            ("{{ len([1, 2]) }}", 2),
            ("{{ max(1, para.n) }}", 3),
            ("{{ min(1, para.n) }}", 1),
            ("{{ round(1.6) }}", 2),
            ("{{ int('4') }}", 4),
            ("{{ str(para.n) }}", "3"),
            ("{{ case.name }}", "C1"),
            ("{{ role.pm }}", "alice"),
        ],
    )
    def test_supported_operations(self, source, expected, ctx):
        assert render(source, ctx) == expected

    def test_whole_string_expression_keeps_native_type(self, ctx):
        # `when` relies on this: it needs a real bool, not the text "False"
        assert render("{{ para.flag }}", ctx) is True
        assert render("{{ para.n }}", ctx) == 3

    def test_embedded_expression_interpolates_to_text(self, ctx):
        assert render("{{ para.n }}H", ctx) == "3H"
        assert render("{{ case.name }} - QA", ctx) == "C1 - QA"

    def test_non_string_passes_through(self, ctx):
        assert render(12, ctx) == 12
        assert render(None, ctx) is None


class TestContainment:
    """Every one of these must be refused; a pass here is a sandbox escape."""

    @pytest.mark.parametrize(
        "source",
        [
            "{{ ().__class__ }}",
            "{{ ().__class__.__bases__ }}",
            "{{ __import__('os') }}",
            "{{ open('/etc/passwd') }}",
            "{{ eval('1') }}",
            "{{ exec('x=1') }}",
            "{{ globals() }}",
            "{{ para.n.__class__ }}",
            "{{ [x for x in range(3)] }}",
            "{{ (lambda: 1)() }}",
            "{{ {'a': 1}['a'] }}",
            "{{ [1, 2][0] }}",
            "{{ para['n'] }}",
            "{{ 1 if True else 2 }}",
            "{{ f'{1}' }}",
            "{{ x := 1 }}",
            "{{ para }}",
            "{{ unknown_name }}",
            # range() went away with for_each; nothing consumes a sequence now
            "{{ range(3) }}",
        ],
    )
    def test_rejected_syntax(self, source, ctx):
        with pytest.raises(DslError):
            render(source, ctx)

    @pytest.mark.parametrize(
        "source",
        [
            "{{ 2 ** 9999999 }}",
            "{{ 'x' * 999999999 }}",
            "{{ [1] * 999999999 }}",
        ],
    )
    def test_resource_exhaustion_is_capped(self, source, ctx):
        with pytest.raises(DslError):
            render(source, ctx)

    def test_unknown_parameter_is_reported_precisely(self, ctx):
        with pytest.raises(DslError) as exc:
            render("{{ para.nope }}", ctx)
        assert exc.value.issues[0].code == "E_UNKNOWN_PARAM"

    def test_division_by_zero_is_a_dsl_error(self, ctx):
        with pytest.raises(DslError):
            render("{{ 1 / 0 }}", ctx)

    def test_syntax_error_is_a_dsl_error(self, ctx):
        with pytest.raises(DslError) as exc:
            render("{{ 1 + }}", ctx)
        assert exc.value.issues[0].code == "E_BAD_EXPRESSION"


class TestReferencedParams:
    def test_collects_names(self):
        found = referenced_params("{{ para.a }} and {{ para.b + para.c }}")
        assert found == {"a", "b", "c"}

    def test_ignores_other_namespaces(self):
        assert referenced_params("{{ case.name }}{{ role.pm }}") == set()

    def test_malformed_expression_is_skipped(self):
        assert referenced_params("{{ para.a }}{{ 1 + }}") == {"a"}
