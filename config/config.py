"""Load and validate ``config/glenwood.yaml``.  # SPEC §10

``Config.load()`` fails fast: a missing key, an unknown key, or an out-of-range
value raises ``ConfigError`` at startup, never at the first deal. Numbers are
parsed straight from the yaml text into ``Decimal`` so ``0.02`` is exactly
``Decimal("0.02")``. ``config_hash`` is a SHA-256 of the validated values in
canonical JSON, so comments and key order do not change it; every ``screens``
and ``underwrites`` row records it.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from schema.models import (
    CourtFlag,
    ExperienceTier,
    Product,
    Severity,
    State,
    Tranche,
    UnderwriteFlag,
)

DEFAULT_PATH = Path(__file__).with_name("glenwood.yaml")

Pct = Annotated[Decimal, Field(ge=0, le=1)]
Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
Years = Annotated[int, Field(ge=0, le=50)]
Months = Annotated[int, Field(ge=0, le=60)]
Fico = Annotated[int, Field(ge=300, le=850)]

CourtAdapter = Literal["oscn", "colorado_courts", "manual"]


class ConfigError(ValueError):
    """Raised when the yaml is missing, malformed, incomplete, or out of range."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreditConfig(_Section):
    """Credit floor and tranche cutoffs.  # SPEC §7.1"""

    floor_tranche: Tranche
    tranche_cutoffs: dict[Tranche, Fico]

    @model_validator(mode="after")
    def _cutoffs_complete_and_descending(self) -> CreditConfig:
        expected = [Tranche.T1, Tranche.T2, Tranche.T3, Tranche.T4]
        if list(self.tranche_cutoffs) != expected:
            raise ValueError(
                "tranche_cutoffs must list exactly T1, T2, T3, T4 in order (T5 is below T4)"
            )
        values = list(self.tranche_cutoffs.values())
        if any(a <= b for a, b in zip(values, values[1:], strict=False)):
            raise ValueError("tranche_cutoffs must be strictly descending from T1 to T4")
        return self


class LeverageCaps(_Section):
    """One cell of the caps grid.  # SPEC §7.4"""

    ltc: Pct
    ltv_as_is: Pct
    ltarv: Pct


CapsGrid = dict[Product, dict[Tranche, dict[ExperienceTier, LeverageCaps]]]


class ScreenConfig(_Section):
    """Verdict tolerances.  # SPEC §7.5"""

    tolerance_band: Pct


class Lookbacks(_Section):
    bankruptcy_years: Years
    satisfied_judgment_years: Years


class Thresholds(_Section):
    """Amount thresholds; judgments aggregate across matters.  # SPEC §7.2

    Open tax liens have no threshold: any open lien is flagged regardless of amount.
    """

    unsatisfied_judgments_aggregate_usd: Money  # sum of unsatisfied judgments
    active_civil_litigation_usd: Money  # per matter


class FlagsConfig(_Section):
    """Court, filing, and underwrite flag severities; lookbacks; thresholds.  # SPEC §7.2, §8.6"""

    severities: dict[CourtFlag, Severity]
    underwrite_severities: dict[UnderwriteFlag, Severity]
    lookbacks: Lookbacks
    thresholds: Thresholds

    @model_validator(mode="after")
    def _every_flag_has_a_severity(self) -> FlagsConfig:
        missing = [f.value for f in CourtFlag if f not in self.severities]
        if missing:
            raise ValueError(f"flags.severities is missing: {', '.join(missing)}")
        missing = [f.value for f in UnderwriteFlag if f not in self.underwrite_severities]
        if missing:
            raise ValueError(f"flags.underwrite_severities is missing: {', '.join(missing)}")
        return self


class ExperienceConfig(_Section):
    """Experience-tier overrides.  # SPEC §7.3"""

    repeat_borrower_min_tier: ExperienceTier


class FeesConfig(_Section):
    """Fee schedule.  # SPEC §3, §8.1, §8.6"""

    origination_pct: Pct
    origination_at_close_pct: Pct
    origination_at_payoff_pct: Pct
    extension_default_pct: Pct
    selling_cost_pct: Pct
    contingency_pct: Pct
    est_closing_pct_of_price: Pct  # screen estimate inside total_cost for LTC (SPEC §7.4)
    borrower_closing_pct_of_price: Pct  # buy-side closing paid in borrower cash (SPEC §8.6)

    @model_validator(mode="after")
    def _origination_split_sums(self) -> FeesConfig:
        if self.origination_at_close_pct + self.origination_at_payoff_pct != self.origination_pct:
            raise ValueError(
                "origination_at_close_pct + origination_at_payoff_pct must equal origination_pct"
            )
        return self


class RateGrid(_Section):
    """Rate axis of the sensitivity grid.  # SPEC §8.5"""

    min: Pct
    max: Pct
    step: Annotated[Decimal, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def _grid_is_well_formed(self) -> RateGrid:
        if self.min >= self.max:
            raise ValueError("rate_grid.min must be below rate_grid.max")
        if (self.max - self.min) % self.step != 0:
            raise ValueError("rate_grid.step must divide (max - min) exactly")
        return self


class MonthWindow(_Section):
    """Month axis of the sensitivity grid: rows term .. term + after_term.  # SPEC §8.5"""

    after_term: Months


class ReturnsConfig(_Section):
    """Lender return target and grid axes.  # SPEC §8.4, §8.5"""

    target_irr: Pct
    rate_grid: RateGrid
    month_window: MonthWindow


class DrawsConfig(_Section):
    """Draw-curve constants.  # SPEC §8.3"""

    draw_avg_utilization: Pct  # average Tranche A utilization over the rehab period
    listing_months: Months  # rehab_months = term - listing_months


class OpexDefaults(_Section):
    """Opex lines used when the deal does not supply them.  # SPEC §8.6

    Rent percentages apply to gross annual rent; value percentages apply to the ARV.
    """

    vacancy_pct_of_rent: Pct
    management_pct_of_rent: Pct
    maintenance_pct_of_rent: Pct
    taxes_pct_of_value: Pct
    insurance_pct_of_value: Pct


class TakeoutConfig(_Section):
    """DSCR takeout assumptions.  # SPEC §8.6"""

    ltv: Pct
    rate: Pct
    amortization_years: Annotated[int, Field(ge=1, le=40)]
    dscr_floor: Annotated[Decimal, Field(gt=0, le=5)]
    opex_defaults: OpexDefaults


class DownsideConfig(_Section):
    """REO downside assumptions.  # SPEC §8.6"""

    reo_haircut: Pct
    foreclosure_cost_usd: Money
    foreclosure_months: dict[State, Months]
    cover_floor: Annotated[Decimal, Field(gt=0, le=5)]

    @model_validator(mode="after")
    def _every_state_has_foreclosure_months(self) -> DownsideConfig:
        missing = [s.value for s in State if s not in self.foreclosure_months]
        if missing:
            raise ValueError(f"downside.foreclosure_months is missing: {', '.join(missing)}")
        return self


class StatesConfig(_Section):
    """States served and the court-record adapter per state.  # SPEC §6, §10"""

    served: list[State]
    court_adapter: dict[State, CourtAdapter]

    @model_validator(mode="after")
    def _served_states_are_real_and_covered(self) -> StatesConfig:
        if not self.served:
            raise ValueError("states.served must list at least one state")
        if State.OTHER in self.served:
            raise ValueError("states.served cannot include OTHER")
        if len(set(self.served)) != len(self.served):
            raise ValueError("states.served has duplicates")
        uncovered = [s.value for s in self.served if s not in self.court_adapter]
        if uncovered:
            raise ValueError(f"states.court_adapter is missing: {', '.join(uncovered)}")
        return self


class Config(_Section):
    """Every GLENWOOD tunable, validated.  # SPEC §10"""

    credit: CreditConfig
    leverage_caps: CapsGrid
    screen: ScreenConfig
    flags: FlagsConfig
    experience: ExperienceConfig
    fees: FeesConfig
    returns: ReturnsConfig
    draws: DrawsConfig
    takeout: TakeoutConfig
    downside: DownsideConfig
    states: StatesConfig

    @model_validator(mode="after")
    def _caps_grid_is_complete(self) -> Config:
        missing: list[str] = []
        for product in Product:
            for tranche in Tranche:
                for tier in ExperienceTier:
                    cell = self.leverage_caps.get(product, {}).get(tranche, {}).get(tier)
                    if cell is None:
                        missing.append(f"{product.value}.{tranche.value}.{tier.value}")
        if missing:
            shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
            raise ValueError(f"leverage_caps is missing {len(missing)} cell(s): {shown}")
        return self

    def canonical_json(self) -> str:
        """Validated values as sorted, compact JSON; the input to ``config_hash``."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        """SHA-256 of ``canonical_json()``; recorded on screens/underwrites.  # SPEC §10"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"invalid config:\n{exc}") from exc

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> Config:
        """Read, parse, and validate the yaml at ``path``; raise ``ConfigError`` on any problem."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
        data = load_yaml(text)
        if not isinstance(data, dict):
            raise ConfigError(f"config {path} must be a mapping at the top level")
        return cls.from_dict(data)


class _DecimalLoader(yaml.SafeLoader):
    """SafeLoader that parses yaml floats as Decimal from their source text."""


def _decimal_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> Decimal:
    text = str(loader.construct_scalar(node))  # type: ignore[arg-type]
    try:
        return Decimal(text.replace("_", ""))
    except InvalidOperation as exc:
        raise ConfigError(f"not a decimal number: {text!r}") from exc


_DecimalLoader.add_constructor("tag:yaml.org,2002:float", _decimal_constructor)


def load_yaml(text: str) -> Any:
    """Parse yaml text with floats as ``Decimal``."""
    try:
        return yaml.load(text, Loader=_DecimalLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed yaml: {exc}") from exc
