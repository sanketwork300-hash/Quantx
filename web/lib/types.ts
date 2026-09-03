export type ResultStatus = "OK" | "PARTIAL" | "FAILED";

export interface AnalyticalWarning {
  code: string;
  severity: "INFO" | "WARNING" | "ERROR";
  message: string;
  context: Record<string, unknown>;
}

export interface Provenance {
  computed_at: string;
  code_commit: string;
  market_state_timestamp: string | null;
  /** The snapshot every number in this result was computed from. */
  market_state_id: string | null;
  market_data_sources: string[];
  dataset_versions: Record<string, string>;
  model_versions: Record<string, string>;
  parameters: Record<string, unknown>;
}

export interface Envelope<T> {
  status: ResultStatus;
  results: T | null;
  warnings: AnalyticalWarning[];
  provenance: Provenance;
}

export interface QualityFlag {
  code: string;
  severity: "INFO" | "WARNING" | "ERROR";
  message: string;
  context: Record<string, unknown>;
}

export interface Quality {
  stale_score: number;
  spread_score: number;
  liquidity_score: number;
  consistency_score: number;
  completeness_score: number;
  overall_score: number;
  flags: QualityFlag[];
}

export interface OptionQuote {
  instrument_id: string;
  expiry: string;
  strike: string;
  option_type: string;
  exchange_timestamp: string;
  bid_price: string | null;
  ask_price: string | null;
  last_price: string | null;
  /** Derived from the observed two-sided market; null when there is none. */
  mid_price: string | null;
  relative_spread: number | null;
  volume: string | null;
  open_interest: string | null;
  underlying_price: string | null;
  source_row_number: number | null;
  excluded: boolean;
  exclusion_reason: string | null;
  quality: Quality;
}

export interface ChainCounts {
  input: number;
  kept: number;
  excluded: number;
  rejected: number;
}

export interface ChainSnapshot {
  snapshot_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  source: string;
  provider: string;
  dataset_digest: string | null;
  underlying_price: string | null;
  counts: ChainCounts;
  quality_summary: Record<string, any>;
  expiries: string[];
  quotes: OptionQuote[];
}

export interface ChainSnapshotSummary {
  snapshot_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  source: string;
  counts: ChainCounts;
  overall_score: number | null;
}

export interface Upload {
  id: string;
  kind: string;
  original_filename: string;
  content_type: string;
  byte_size: number;
  sha256: string;
  status: string;
  created_at: string;
}

export interface Preview {
  upload_id: string;
  headers: string[];
  inferred_mapping: Record<string, string>;
  applied_mapping: Record<string, string>;
  missing_required: string[];
  unmapped_columns: string[];
  sample_rows: Record<string, unknown>[];
  parse_errors: { row_number: number; column: string | null; message: string }[];
}

export interface Job {
  job_id: string;
  job_type: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  progress: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: Record<string, unknown> | null;
}

export interface JobResult {
  job_id: string;
  status: string;
  result: Envelope<IngestionSummary> | null;
  error: Record<string, unknown> | null;
}

export interface IngestionSummary {
  snapshot_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  counts: ChainCounts;
  exclusion_counts: Record<string, number>;
  rejection_counts: Record<string, number>;
  flag_counts: Record<string, number>;
  aggregate_quality: Quality;
  rejected_rows: { row_number: number; reason: string; message: string }[];
  expiries: string[];
}

export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string | null;
  correlation_id: string | null;
  errors?: unknown[];
}

/* ------------------------------------------------------------ derivatives */

export interface ForwardEstimate {
  method: string;
  selected: boolean;
  value: number | null;
  confidence: number;
  observations: number;
  residual_error: number | null;
  discount_factor: number | null;
  error: string | null;
  assumptions: string[];
}

export interface ImpliedVolPoint {
  instrument_id: string;
  expiry: string;
  strike: string;
  option_type: string;
  price_used: number | null;
  price_source: string;
  /** Implied by the observed price. The fitted reference IV is a Phase 2 field. */
  market_iv: number | null;
  market_iv_bid: number | null;
  market_iv_ask: number | null;
  iv_envelope_width: number | null;
  converged: boolean;
  iterations: number;
  solver: string;
  error: string | null;
  vega: number | null;
  /** Volatility uncertainty from one price ulp; do not display beyond it. */
  uncertainty: number | null;
  time_to_expiry: number | null;
  log_moneyness: number | null;
  total_variance: number | null;
  weight: number;
  used_for_smile: boolean;
  smile_exclusion: string | null;
}

export interface SmileSlice {
  expiry: string;
  time_to_expiry: number | null;
  settlement_time_assumed: boolean;
  forward: {
    selected: ForwardEstimate | null;
    estimates: ForwardEstimate[];
    disagreement: number | null;
  };
  counts: { quotes: number; solved: number; used_for_smile: number };
  solve_failures: Record<string, number>;
  atm_volatility: number | null;
  skew: number | null;
  curvature: number | null;
  reason: string | null;
  points: ImpliedVolPoint[];
}

export interface ChainAnalysis {
  analysis_id: string;
  snapshot_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  underlying_price: string | null;
  curve_id: string | null;
  counts: { quotes: number; solved: number; expiries: number };
  slices: SmileSlice[];
}

export interface AnalysisSummary {
  analysis_id: string;
  snapshot_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  curve_id: string | null;
  quotes_in: number;
  quotes_solved: number;
  expiries: number;
  created_at: string;
}

/* -------------------------------------------------- volatility surface (P2) */

export interface SVIParameters {
  a: number;
  b: number;
  rho: number;
  m: number;
  sigma: number;
}

export interface CalibrationMetrics {
  status: string;
  n_observations: number;
  rmse_total_variance: number | null;
  weighted_rmse: number | null;
  rmse_vol_points: number | null;
  max_error_vol_points: number | null;
  optimizer: string;
  optimizer_message: string | null;
  iterations: number;
  starts_attempted: number;
  starts_feasible: number;
  /** Minimum of Durrleman's g; negative means a negative implied density. */
  min_durrleman_g: number | null;
  min_durrleman_k: number | null;
  wing_slope: number | null;
  constraints_satisfied: boolean;
  error: string | null;
}

export interface SurfaceSlice {
  expiry: string;
  time_to_expiry: number;
  forward: number;
  discount_factor: number;
  forward_method: string | null;
  forward_confidence: number;
  /** Log-moneyness actually fitted; outside it a lookup is extrapolation. */
  k_min: number | null;
  k_max: number | null;
  parameters: SVIParameters | null;
  calibration: CalibrationMetrics;
}

export interface Surface {
  surface_id: string;
  surface_row_id?: string;
  underlying_id: string;
  as_of_timestamp: string;
  model: string;
  model_version: string;
  curve_id: string | null;
  analysis_id: string | null;
  counts: { slices: number; fitted: number };
  slices: SurfaceSlice[];
}

export interface SurfaceSummary {
  surface_row_id: string;
  surface_id: string;
  underlying_id: string;
  analysis_id: string;
  as_of_timestamp: string;
  model: string;
  model_version: string;
  curve_id: string | null;
  slices_total: number;
  slices_fitted: number;
  created_at: string;
}

export interface ArbitrageViolation {
  scope: string;
  type: string;
  severity: "INFO" | "WARNING" | "ERROR";
  /** How far the condition is breached, in that condition's own units. */
  magnitude: number;
  tolerance: number | null;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  detail: Record<string, unknown>;
  affected_instruments: string[];
}

export interface ArbitrageReport {
  scope: string;
  severity: string | null;
  violations_total: number;
  observations: number;
  checks_run: string[];
  summary: { by_type?: Record<string, number>; by_severity?: Record<string, number> };
  violations: ArbitrageViolation[];
}

export interface ArbitrageResults {
  analysis_id: string;
  raw_market: ArbitrageReport | null;
  fitted_surface: ArbitrageReport | null;
}

/* -------------------------------------------------- anomaly analytics (P3) */

export interface Explanation {
  factor: string;
  /** Effect on confidence in the measurement — never on a trade. */
  effect: "SUPPORTS" | "REDUCES" | "NEUTRAL";
  detail: string;
  value: number | null;
}

export interface SurfaceAnomaly {
  instrument_id: string;
  expiry: string;
  strike: string;
  option_type: string;
  /** Implied by the observed price. */
  market_iv: number;
  /** Produced by the fitted surface. A model output, never a fair value. */
  reference_iv: number;
  iv_difference: number;
  iv_difference_vol_points: number;
  relative_deviation: number;
  market_iv_bid: number | null;
  market_iv_ask: number | null;
  envelope_position: "INSIDE" | "ABOVE_ASK" | "BELOW_BID" | "UNKNOWN";
  excess_over_envelope: number;
  /** Everything that could account for the difference, combined. */
  explained_scale: number;
  z_score: number;
  historical_z_score: number | null;
  historical_observations: number;
  liquidity_score: number;
  data_quality_score: number;
  calibration_rmse_vol_points: number | null;
  iv_uncertainty: number | null;
  reference_method: string;
  reference_flags: string[];
  confidence: number;
  flagged: boolean;
  explanation: Explanation[];
}

export interface AnomalyScan {
  scan_id: string;
  surface_id: string;
  analysis_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  counts: { examined: number; scored: number; flagged: number; returned: number };
  policy: Record<string, unknown>;
  anomalies: SurfaceAnomaly[];
}

export interface Distribution {
  count: number;
  mean: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
  median: number | null;
  p10: number | null;
  p90: number | null;
  is_reliable: boolean;
}

export interface CharacteristicPercentile {
  name: string;
  current: number | null;
  percentile: number | null;
  z_score: number | null;
  distribution: Distribution;
  is_reliable: boolean;
}

export interface TenorHistory {
  tenor_days: number;
  as_of_timestamp: string | null;
  observations: number;
  is_reliable: boolean;
  minimum_reliable_observations: number;
  percentiles: CharacteristicPercentile[];
  series: Record<string, number | string>[];
}

export interface SurfaceHistory {
  underlying_id: string;
  tenors: TenorHistory[];
}

// ---------------------------------------------------------------- portfolio
export interface Portfolio {
  id: string;
  name: string;
  base_currency: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface Position {
  id: string;
  portfolio_id: string;
  instrument_id: string;
  quantity: string;
  side: string;
  average_price: string | null;
  source: string;
  strategy_tag: string | null;
  metadata: Record<string, unknown>;
}

export interface ResolvedImportRow {
  row_number: number;
  instrument_id: string;
  canonical_key: string;
  symbol: string;
  asset_class: string;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  quantity: string;
  side: string;
  average_price: string | null;
  strategy_tag: string | null;
  multiplier: string;
  currency: string;
  resolution_method: string;
  creates_instrument: boolean;
  creates_underlying: string | null;
  multiplier_is_assumed: boolean;
}

export interface AmbiguousImportRow {
  row_number: number;
  reason: string;
  candidates: {
    instrument_id: string;
    canonical_key: string;
    symbol: string;
    expiry: string | null;
    strike: string | null;
    option_type: string | null;
  }[];
  raw: Record<string, string>;
}

export interface InvalidImportRow {
  row_number: number;
  reason: string;
  message: string;
}

export interface ImportPreview {
  upload_id: string;
  headers: string[];
  inferred_mapping: Record<string, string>;
  applied_mapping: Record<string, string>;
  rows_in: number;
  committable: boolean;
  resolved: ResolvedImportRow[];
  ambiguous: AmbiguousImportRow[];
  invalid: InvalidImportRow[];
}

export interface PortfolioGreeks {
  delta: number;
  gamma: number;
  vega_per_vol_point: number;
  theta_per_day: number;
  rho_per_bp: number;
  units: Record<string, string>;
}

export interface AggregateBucket {
  dimension: string;
  key: string;
  label: string;
  positions: number;
  valued: number;
  base_market_value: string;
  gross_exposure: string;
  net_exposure: string;
  unrealized_pnl: string;
  greeks: PortfolioGreeks;
}

export interface PortfolioValuation {
  valuation_id: string;
  portfolio_id: string;
  as_of_timestamp: string;
  base_currency: string;
  market_state_id: string | null;
  positions: number;
  valued: number;
  base_market_value: string;
  unrealized_pnl: string;
  gross_exposure: string;
  net_exposure: string;
  greeks: PortfolioGreeks;
  valuation_methods: Record<string, number>;
  aggregates: AggregateBucket[];
  created_at: string;
}

export interface PositionValuationDetail {
  position_id: string;
  instrument_id: string;
  canonical_key: string;
  asset_class: string;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  quantity: string;
  multiplier: string;
  currency: string;
  /** Observed. Never written from the model. */
  market_price: string | null;
  /** Estimated from the fitted surface. Never written from the market. */
  model_price: string | null;
  price_used: string | null;
  valuation_method: string;
  market_value: string | null;
  base_market_value: string | null;
  fx_rate: string | null;
  unrealized_pnl: string | null;
  greeks: PortfolioGreeks;
  greek_source: string;
  implied_volatility: number | null;
  time_to_expiry: number | null;
  quote_age_seconds: number | null;
  warnings: string[];
}

export interface ValuationDetail extends PortfolioValuation {
  positions_detail: PositionValuationDetail[];
}

// --------------------------------------------------------------------- risk
export type ShockType =
  | "ABSOLUTE"
  | "PERCENTAGE"
  | "VOL_POINTS"
  | "BASIS_POINTS";

export type RiskFactorKind =
  | "UNDERLYING_PRICE"
  | "VOLATILITY"
  | "RISK_FREE_RATE"
  | "DIVIDEND_YIELD"
  | "FX_RATE";

export interface Shock {
  kind: RiskFactorKind;
  shock_type: ShockType;
  value: number;
  target: string | null;
  label: string;
}

export interface HistoricalDerivation {
  series: string;
  observations: number;
  start_date: string;
  end_date: string;
  event_date: string;
  window_days: number;
  method: string;
}

export interface Scenario {
  id: string;
  name: string;
  description: string | null;
  /** HYPOTHETICAL templates make no claim about any market's past. */
  source: "HYPOTHETICAL" | "DERIVED_FROM_HISTORY" | "USER_DEFINED";
  shocks: Shock[];
  derivation: HistoricalDerivation | null;
  created_at: string | null;
}

export interface TailRisk {
  confidence: number;
  value_at_risk: number;
  expected_shortfall: number;
  observations: number;
  tail_observations: number;
  quantile_method: string;
  mean_loss: number;
  worst_loss: number;
  is_reliable: boolean;
  warnings: string[];
  interpretation: { value_at_risk: string; expected_shortfall: string };
}

export interface FactorPanelSummary {
  policy: string;
  window_days: number;
  observations: number;
  aligned_levels: number;
  date_range: [string, string] | null;
  factors: {
    name: string;
    kind: string;
    target: string;
    source: string;
    observations: number;
    start_date: string | null;
    end_date: string | null;
  }[];
  missing_data_policy: string;
  warnings: string[];
}

export interface VaRResult {
  var_id: string;
  method: string;
  horizon_days: number;
  base_value: number;
  scenarios: number;
  tail_risk: TailRisk[];
  estimate_intervals: Record<
    string,
    { low: number; high: number; interval: number }
  >;
  assumptions: Record<string, unknown>;
  factor_panel: FactorPanelSummary;
  worst_scenario_dates: string[];
  warnings: string[];
}

export interface RiskContribution {
  dimension: string;
  key: string;
  positions: number;
  base_value: number;
  contribution: number;
  share: number | null;
}

export interface ContributionBreakdown {
  dimension: string;
  total_pnl: number;
  residual: number;
  residual_note: string;
  ungrouped_positions: number;
  ungrouped_pnl: number;
  contributions: RiskContribution[];
}

export interface StressPosition {
  position_id: string;
  canonical_key: string;
  underlying_id: string;
  strategy_tag: string | null;
  expiry: string | null;
  asset_class: string;
  base_value: number;
  shocked_value: number;
  pnl: number;
  shocked_spot: number;
  shocked_volatility: number | null;
  volatility_was_floored: boolean;
}

export interface StressResult {
  stress_id: string;
  scenario: { id: string; name: string; source: string };
  base_value: number;
  shocked_value: number;
  /** The full repricing. This is the answer. */
  pnl: number;
  greek_approximation: {
    /** A second-order estimate, kept beside the answer, never in place of it. */
    pnl: number;
    difference_from_full_revaluation: number;
    method: string;
    caveat: string;
  };
  time_decay_days: number;
  floored_volatilities: number;
  shocks: Record<string, Record<string, number>>;
  positions: StressPosition[];
  contributions: ContributionBreakdown[];
  excluded_positions: number;
  excluded_reported_value: number;
}

export interface VaRSummary {
  id: string;
  portfolio_id: string;
  snapshot_id: string;
  method: string;
  horizon_days: number;
  scenarios: number;
  base_value: number;
  seed: number | null;
  tail_risk: TailRisk[];
  warnings: string[];
  created_at: string;
}

export interface StressSummary {
  id: string;
  portfolio_id: string;
  snapshot_id: string;
  scenario_name: string;
  scenario_source: string;
  base_value: number;
  shocked_value: number;
  pnl: number;
  greek_estimate: number;
  time_decay_days: number;
  created_at: string;
}

// ------------------------------------------------------------------- margin
export interface MarginComponent {
  name: string;
  amount: number;
  /** What the component is, in a sentence a reader can argue with. */
  basis: string;
}

export interface MarginModelInfo {
  name: string;
  version: string;
  description: string;
  /** False for every model here, and only ever true for a published methodology. */
  is_broker_equivalent: boolean;
}

export interface LadderPoint {
  spot_return: number;
  vol_points: number;
  portfolio_value: number;
  pnl: number;
  available_capital: number | null;
  estimated_margin: number;
  margin_confidence: number;
  buffer: number | null;
  utilisation: number | null;
  in_shortfall: boolean;
}

export interface ShortfallRegion {
  direction: "DOWNSIDE" | "UPSIDE";
  approximate_entry: number;
  bracketed_by: [number, number];
  buffer_before: number;
  buffer_after: number;
}

export interface MarginEstimate {
  method: string;
  model_version: string;
  estimated_margin: number;
  currency: string;
  components: MarginComponent[];
  assumptions: string[];
  confidence: number;
  parameters: {
    grid: { spot_returns: number[]; vol_points: number[]; points: number };
    short_option_minimum_rate: number;
    concentration_add_on_rate: number;
    concentration_threshold: number;
  };
  worst_case: {
    spot_return: number;
    vol_points: number;
    loss: number;
    at_grid_edge: boolean;
  };
  positions: number;
  excluded_positions: number;
  warnings: string[];
  disclaimer: string;
}

export interface MarginResult {
  margin_id: string;
  method: string;
  currency: string;
  eligible_capital: number | null;
  vol_co_shock: number;
  estimated_margin: number;
  margin: MarginEstimate;
  buffer: number | null;
  utilisation: number | null;
  in_shortfall_at_rest: boolean;
  shortfall_region: {
    downside: ShortfallRegion | null;
    upside: ShortfallRegion | null;
  };
  summary: string;
  assumptions: string[];
  warnings: string[];
  ladder: LadderPoint[];
}

export interface MarginSummary {
  id: string;
  portfolio_id: string;
  snapshot_id: string;
  method: string;
  model_version: string;
  currency: string;
  estimated_margin: number;
  confidence: number;
  eligible_capital: number | null;
  buffer: number | null;
  utilisation: number | null;
  in_shortfall_at_rest: boolean;
  vol_co_shock: number;
  worst_spot_return: number;
  worst_vol_points: number;
  worst_loss: number;
  worst_at_grid_edge: boolean;
  positions: number;
  excluded_positions: number;
  summary: string;
  warnings: string[];
  created_at: string;
}

// -------------------------------------------------------------- execution
export interface ResolvedTradeRow {
  row_number: number;
  instrument_id: string;
  canonical_key: string;
  symbol: string;
  asset_class: string;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  side: string;
  quantity: string;
  price: string;
  timestamp: string;
  parent_order_key: string | null;
  fees: string;
  resolution_method: string;
  creates_instrument: boolean;
  creates_underlying: string | null;
  multiplier_is_assumed: boolean;
}

export interface TradeImportPreview {
  upload_id: string;
  headers: string[];
  inferred_mapping: Record<string, string>;
  applied_mapping: Record<string, string>;
  rows_in: number;
  committable: boolean;
  resolved: ResolvedTradeRow[];
  ambiguous: AmbiguousImportRow[];
  invalid: InvalidImportRow[];
}

export interface BenchmarkOut {
  kind: string;
  price: string | null;
  available: boolean;
  method: string;
  window: { start: string | null; end: string | null };
  source: string | null;
  observations: number;
  /** Populated whenever `available` is false. Never a silent null. */
  unavailable_reason: string | null;
  flags: string[];
}

export interface ShortfallOut {
  benchmark: string;
  benchmark_price: string;
  average_price: string;
  currency_amount: string;
  basis_points: number;
  percent: number;
  currency: string;
  quantity: string;
  multiplier: string;
  convention: string;
}

export interface CostComponentOut {
  name: string;
  amount: string | null;
  status: "MEASURED" | "MODELLED" | "RESIDUAL" | "NOT_MODELLED" | "UNAVAILABLE";
  basis: string;
}

export interface CostDecompositionOut {
  benchmark: string;
  total: string;
  currency: string;
  components: CostComponentOut[];
  caveat: string;
}

export interface MarketWindowOut {
  instrument_id: string;
  start: string;
  end: string;
  source: string;
  staleness_tolerance_seconds: number;
  coverage: {
    observations: number;
    window_seconds: number;
    span_ratio: number;
    largest_gap_seconds: number;
    brackets_start: boolean;
    brackets_end: boolean;
    has_interval_volume: boolean;
    is_sufficient: boolean;
    minimum_observations: number;
    minimum_span_ratio: number;
    policy: string;
  };
}

export interface ParentOrderOut {
  key: string;
  instrument_id: string;
  canonical_key: string | null;
  symbol: string | null;
  side: string;
  grouping_method: string;
  /** True when the platform grouped the fills itself. Changes every benchmark. */
  grouping_is_inferred: boolean;
  fills: number;
  filled_quantity: string;
  multiplier: string;
  currency: string;
  average_price: string;
  order_quantity: string | null;
  unfilled_quantity: string | null;
  fees: string;
  start: string;
  end: string;
  duration_seconds: number;
  has_submit_timestamp: boolean;
  decision_timestamp: string | null;
}

export interface ExecutionAnalysisOut {
  parent_order: ParentOrderOut;
  primary_benchmark: string;
  benchmarks: BenchmarkOut[];
  shortfalls: ShortfallOut[];
  unavailable_shortfalls: { benchmark: string; available: false; reason: string }[];
  decomposition: CostDecompositionOut | null;
  market_window: MarketWindowOut;
  warnings: string[];
}

export interface ExecutionAnalysisResult {
  parent_orders: number;
  fills: number;
  reports: ExecutionAnalysisOut[];
  report_ids: string[];
}

export interface ExecutionReportSummary {
  id: string;
  instrument_id: string;
  parent_order_key: string;
  grouping_method: string;
  grouping_is_inferred: boolean;
  side: string;
  canonical_key: string | null;
  currency: string;
  multiplier: string;
  fills: number;
  filled_quantity: string;
  order_quantity: string | null;
  average_price: string;
  fees: string;
  window_start: string;
  window_end: string;
  primary_benchmark: string;
  primary_benchmark_price: string | null;
  shortfall_currency: number | null;
  shortfall_bps: number | null;
  shortfall_percent: number | null;
  observations: number;
  coverage_span_ratio: number;
  coverage_is_sufficient: boolean;
  warnings: string[];
  created_at: string;
}

export interface ExecutionRow {
  id: string;
  instrument_id: string;
  side: string;
  quantity: string;
  execution_price: string;
  exchange_timestamp: string;
  order_id: string | null;
  parent_order_key: string | null;
  order_type: string;
  limit_price: string | null;
  order_quantity: string | null;
  submit_timestamp: string | null;
  decision_timestamp: string | null;
  broker: string | null;
  venue: string | null;
  fees: string;
  source: string;
  created_at: string;
}

// -------------------------------------------------- execution simulation
export interface StrategyInfo {
  name: string;
  version: string;
  description: string;
  /** What the caller must supply. The platform supplies none of it. */
  requires: string[];
}

export interface ImpactModelInfo {
  name: string;
  version: string;
  description: string;
  /** False for every model until you supply a coefficient of your own. */
  ships_calibrated_coefficients: boolean;
}

export interface ScheduleSliceOut {
  index: number;
  start: string;
  end: string;
  quantity: string;
  participation: number | null;
}

export interface ScheduleOut {
  strategy: string;
  side: string;
  parent_quantity: string;
  slices: number;
  start: string;
  end: string;
  duration_seconds: number;
  peak_participation: number | null;
  parameters: Record<string, unknown>;
  assumptions: string[];
  warnings: string[];
  slice_detail?: ScheduleSliceOut[];
}

export interface SimulatedFillOut {
  index: number;
  timestamp: string;
  quantity: string;
  observed_price: string;
  drifted_price: string;
  fill_price: string;
  spread_cost_per_unit: string;
  temporary_impact_per_unit: string;
  permanent_impact_per_unit: string;
  participation: number | null;
  price_age_seconds: number | null;
}

export interface UnfilledSliceOut {
  index: number;
  timestamp: string;
  quantity: string;
  reason: string;
}

export interface SimulatedStrategyOut {
  /** Always true. A database CHECK makes anything else unstorable. */
  counterfactual: boolean;
  caveat: string;
  strategy: string;
  impact_model: string;
  side: string;
  ordered_quantity: string;
  filled_quantity: string;
  completion_rate: number;
  average_price: string | null;
  time_to_completion_seconds: number | null;
  modelled_impact_cost: string;
  modelled_spread_cost: string;
  latency_seconds: number;
  max_price_age_seconds: number;
  shortfall: ShortfallOut | null;
  benchmarks: BenchmarkOut[];
  schedule: ScheduleOut;
  context: Record<string, unknown>;
  unfilled: UnfilledSliceOut[];
  fills?: SimulatedFillOut[];
  warnings: string[];
}

export interface StrategyComparisonOut {
  counterfactual: boolean;
  caveat: string;
  comparison_caveat: string;
  comparison_id: string;
  window: { start: string; end: string };
  strategies: SimulatedStrategyOut[];
  unavailable: { strategy: string; reason: string }[];
}

export interface SimulationSummary {
  id: string;
  comparison_id: string;
  instrument_id: string;
  counterfactual: boolean;
  strategy: string;
  impact_model: string;
  impact_is_calibrated: boolean;
  side: string;
  ordered_quantity: string;
  filled_quantity: string;
  completion_rate: number;
  average_price: string | null;
  window_start: string;
  window_end: string;
  latency_seconds: number;
  max_price_age_seconds: number;
  modelled_impact_cost: string;
  modelled_spread_cost: string;
  primary_benchmark: string | null;
  shortfall_currency: number | null;
  shortfall_bps: number | null;
  warnings: string[];
  created_at: string;
}

export interface Instrument {
  id: string;
  canonical_key: string;
  asset_class: string;
  exchange: string;
  symbol: string;
  underlying_id: string | null;
  currency: string;
  multiplier: string;
  tick_size: string;
  lot_size: string;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  status: string;
}

// --------------------------------------------------------------- Phase 9
export interface SSVIParameters {
  rho: number;
  eta: number;
  gamma: number;
}

export interface GlobalSurfaceSummary {
  global_surface_row_id: string;
  surface_id: string;
  underlying_id: string;
  analysis_id: string;
  as_of_timestamp: string;
  model: string;
  model_version: string;
  status: string;
  curve_id: string | null;
  parameters: SSVIParameters | null;
  n_slices: number;
  n_observations: number;
  rmse_vol_points: number | null;
  min_durrleman_g: number | null;
  max_butterfly_quantity: number | null;
  butterfly_bounds_satisfied: boolean;
  /** Structural for SSVI: a non-decreasing variance term structure cannot
   *  contain calendar arbitrage. Not a diagnostic that happened to pass. */
  calendar_arbitrage_free: boolean;
  created_at: string;
}

export interface GlobalSurfaceSlice {
  expiry: string;
  time_to_expiry: number;
  forward: number;
  discount_factor: number;
  theta: number;
  atm_volatility: number | null;
  forward_method: string | null;
  forward_confidence: number;
  diagnostics: {
    n_observations: number;
    rmse_vol_points: number;
    max_error_vol_points: number;
    k_min: number;
    k_max: number;
    butterfly_first: number;
    butterfly_second: number;
    butterfly_bounds_satisfied: boolean;
    min_durrleman_g: number;
  } | null;
}

export interface GlobalSurface {
  global_surface_row_id: string;
  surface_id: string;
  underlying_id: string;
  as_of_timestamp: string;
  model: string;
  model_version: string;
  curve_id: string | null;
  analysis_id: string | null;
  underlying_price: number | null;
  carry: number;
  status: string;
  parameters: SSVIParameters | null;
  term_structure: {
    maturities: number[];
    thetas: number[];
    is_monotone: boolean;
  } | null;
  slices: GlobalSurfaceSlice[];
  diagnostics: {
    n_observations: number;
    n_slices: number;
    rmse_vol_points: number | null;
    max_error_vol_points: number | null;
    min_durrleman_g: number | null;
    max_butterfly_quantity: number | null;
    butterfly_bounds_satisfied: boolean;
    calendar_arbitrage_free: boolean;
    starts_attempted: number;
    starts_feasible: number;
    optimizer: string;
    optimizer_message: string | null;
    error: string | null;
  };
}

export interface LocalVolatilitySurface {
  local_volatility_row_id: string;
  global_surface_row_id: string;
  as_of_timestamp: string;
  model_version: string;
  spot: number;
  carry: number;
  total_points: number;
  valid_points: number;
  /** Grid points where Dupire's formula produced nothing. Kept as holes with
   *  their reasons rather than interpolated over. */
  flagged_points: number;
  coverage: number;
  flag_counts: Record<string, number>;
  log_moneyness: number[];
  maturities: number[];
  /** `null` marks a hole; a plot must not draw a line through one. */
  values: (number | null)[][];
}

export interface RiskNeutralDensity {
  density_row_id: string;
  expiry: string;
  time_to_expiry: number;
  forward: number;
  discount_factor: number;
  total_mass: number;
  implied_mean: number;
  negative_mass: number;
  mean_error: number;
  is_admissible: boolean;
  flags: string[];
  percentiles: Record<string, number | null>;
  strikes: number[];
  density: number[];
}

export interface HestonCalibration {
  heston_calibration_row_id: string;
  as_of_timestamp: string;
  model_version: string;
  status: string;
  v0: number | null;
  kappa: number | null;
  theta: number | null;
  xi: number | null;
  rho: number | null;
  n_observations: number;
  n_maturities: number;
  rmse_vol_points: number | null;
  max_error_vol_points: number | null;
  feller: number | null;
  satisfies_feller: boolean;
  feller_enforced: boolean;
  warnings: string[];
  error: string | null;
}

export interface ModelValue {
  model: string;
  model_version: string;
  /** Exactly one of these two is set, enforced by a database constraint. */
  value: number | null;
  unavailable_reason: string | null;
  method: string;
  inputs_used: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
  warnings: string[];
}

export interface ConfidenceContribution {
  name: string;
  score: number;
  weight: number;
  basis: string;
}

export interface ModelConsensus {
  consensus_row_id: string;
  global_surface_row_id: string | null;
  instrument_id: string;
  expiry: string;
  strike: string;
  option_type: string;
  as_of_timestamp: string;
  model_version: string;
  spot: number;
  time_to_expiry: number;
  risk_free_rate: number;
  dividend_yield: number;
  reference_volatility: number | null;
  models_requested: number;
  models_available: number;
  /** The median of the models that produced a value. Not a price the contract
   *  is worth, and deliberately less prominent than the range around it. */
  reference_value: number | null;
  reference_range: [number, number] | null;
  model_dispersion: {
    absolute: number | null;
    relative: number | null;
    standard_deviation: number | null;
  };
  market_price: number | null;
  market_deviation: number | null;
  market_deviation_relative: number | null;
  confidence: number;
  confidence_contributions: ConfidenceContribution[];
  vanna: number | null;
  volga: number | null;
  charm_per_day: number | null;
  values: ModelValue[];
  created_at: string;
}

// --------------------------------------------------------- microstructure (P10)

/** One capability verdict, with the evidence it was decided on.
 *
 *  `reason` is null exactly when `available` is true. Render the message; a
 *  refusal that shows only the reason code tells the reader nothing about what
 *  their feed was missing.
 */
export interface CapabilityVerdict {
  capability: string;
  available: boolean;
  reason: string | null;
  message: string;
  evidence: Record<string, unknown>;
}

export interface MicrostructureDataset {
  id: string;
  instrument_id: string;
  name: string;
  kind: string;
  source: string;
  snapshot_rows_in: number;
  snapshot_rows_kept: number;
  snapshot_rows_rejected: number;
  event_rows_in: number;
  event_rows_kept: number;
  event_rows_rejected: number;
  rejection_counts: Record<string, number>;
  first_timestamp: string | null;
  last_timestamp: string | null;
  span_seconds: number;
  max_depth_levels: number;
  available_capabilities: string[];
  created_at: string;
}

export interface DatasetDetail {
  dataset_id: string;
  instrument_id: string;
  name: string;
  kind: string;
  source: string;
  rows: {
    snapshots: { input: number; kept: number; rejected: number };
    events: { input: number; kept: number; rejected: number };
    rejection_counts: Record<string, number>;
  };
  window: { start: string | null; end: string | null; span_seconds: number };
  max_depth_levels: number;
  availability: {
    gate_version: string;
    capabilities: CapabilityVerdict[];
    available: string[];
    refused: string[];
    thresholds: Record<string, number>;
    profile: Record<string, unknown>;
  };
}

export interface DatasetPreview {
  committable: boolean;
  snapshots: {
    input: number;
    kept: number;
    rejected: number;
    detected_levels: number;
    rejected_sample: { row_number: number; reason: string; message: string }[];
  };
  events: {
    input: number;
    kept: number;
    rejected: number;
    rejected_sample: { row_number: number; reason: string; message: string }[];
  };
  detected_snapshot_columns: {
    timestamp: string | null;
    receive_timestamp: string | null;
    sequence: string | null;
    depth: number;
    levels: Record<string, Record<string, Record<string, string>>>;
    unrecognised_columns: string[];
  };
  detected_event_mapping: Record<string, string>;
  availability: DatasetDetail["availability"];
}

/** One measure over the session. `observations + missing` is always the number
 *  of snapshots analysed, so an average is never quietly over a subset. */
export interface MeasureSummary {
  measure: string;
  observations: number;
  missing: number;
  missing_reasons: Record<string, number>;
  mean: number | null;
  percentiles: Record<string, number>;
  minimum: number | null;
  maximum: number | null;
}

export interface TradeCostSummary {
  quantity: number;
  side: string;
  snapshots_that_could_absorb_it: number;
  snapshots_that_could_not: number;
  median_slippage_bps: number | null;
  p95_slippage_bps: number | null;
  median_levels_consumed: number | null;
  note: string;
}

export interface BookAnalyticsReport {
  report_id: string;
  dataset_id: string;
  levels: number;
  weighted_decay: number;
  snapshots_analysed: number;
  window: { start: string | null; end: string | null };
  crossed_snapshots: number;
  locked_snapshots: number;
  measures: MeasureSummary[];
  trade_costs: TradeCostSummary[];
  series: Record<string, number | string | null>[];
  series_note: string;
}

export interface IntensityFitOut {
  parameters: Record<string, number | string | null>;
  log_likelihood: number;
  log_likelihood_per_event: number | null;
  events: number;
  window_seconds: number;
  converged: boolean;
  ks_statistic: number | null;
}

/** Both models, always. Read `hawkes_is_adopted` before rendering any Hawkes
 *  parameter: when it is false those parameters describe a candidate the
 *  held-out test rejected. */
export interface IntensityComparison {
  intensity_model_id?: string;
  dataset_id: string;
  scope?: string;
  events_selected: number;
  window: { start: string; end: string; split: string };
  held_out_events: number;
  poisson: { train: IntensityFitOut; held_out_log_likelihood: number };
  hawkes: { train: IntensityFitOut; held_out_log_likelihood: number };
  log_likelihood_gain: number;
  log_likelihood_gain_per_event: number | null;
  predictive_test: {
    test: string;
    variance_estimator: string;
    mean_gain_per_event: number;
    standard_error: number;
    statistic: number;
    critical_value: number;
    significant: boolean;
    events: number;
    newey_west_lags: number;
  };
  hawkes_is_adopted: boolean;
  adopted_model: string;
  adopted_rate_per_second: number;
  reason: string;
  method: string;
  interpretation: string;
}

/** A bracket. There is no single fill probability at the top level, and no
 *  field that could hold one. */
export interface QueueOutlookOut {
  queue_estimate_id?: string;
  dataset_id: string;
  estimated_queue_position: number;
  queue_position_fraction_of_displayed_size: number | null;
  level_quantity: number;
  price: string;
  snapshot_timestamp: string;
  horizon_seconds: number;
  observation_window_seconds: number;
  trades_observed: number;
  cancels_observed: number;
  estimated_fill_probability_range: [number, number];
  estimated_wait_seconds_range: [number, number];
  optimistic: QueueEnd;
  pessimistic: QueueEnd;
  assumptions: string[];
  confidence: number;
  interpretation: string;
}

export interface QueueEnd {
  priority_assumption: string;
  quantity_ahead: number;
  departure_rate_per_second: number;
  event_rate_per_second: number;
  mean_event_size: number;
  events_required: number;
  estimated_wait_seconds: number;
  estimated_fill_probability: number;
  horizon_seconds: number;
}

export interface CapabilityReference {
  capability: string;
  measures: string[];
  requires: string;
}

// --------------------------------------------------------------- Phase 11
/** A listing with its page metadata, as the instrument routes return it. */
export interface Listing<T> {
  items: T[];
  meta: { limit: number; offset: number; count: number };
}

/** The one snapshot every branch of an order analysis reads. */
export interface MarketStateOut {
  state_id: string;
  as_of_timestamp: string;
  counts: {
    quotes: number;
    spot_prices: number;
    yield_curves: number;
    volatility_surfaces: number;
  };
  sources: string[];
  dataset_versions: Record<string, string>;
}

/**
 * One quantity, before and after the proposed order. `change` is derived by
 * the server from the two sides; there is no field for a change that was not
 * computed as a difference.
 */
export interface Movement {
  name: string;
  unit: string;
  current: number | null;
  proposed: number | null;
  change: number | null;
}

export interface ObservedMarket {
  available: boolean;
  reason?: string;
  bid?: string | null;
  ask?: string | null;
  mid?: string | null;
  last?: string | null;
  spread?: string | null;
  exchange_timestamp?: string;
  age_seconds?: number | null;
  source?: string;
  quality?: Quality | null;
  note?: string;
}

export interface StrategyCostOut {
  strategy: string;
  impact_model: string | null;
  slices: number;
  average_fill_price: string;
  estimated_slippage_per_unit: string | null;
  estimated_slippage_currency: number | null;
  estimated_slippage_basis_points: number | null;
  spread_component_currency: number | null;
  impact_component_currency: number | null;
  peak_participation: number | null;
  assumptions: string[];
  warnings: string[];
}

export interface OrderCostOut {
  model_version: string;
  side: string;
  quantity: string;
  currency: string;
  reference_price: string;
  quoted_spread: string | null;
  marketability: "MARKETABLE" | "PASSIVE" | "UNKNOWN";
  marketability_basis: string;
  strategies: StrategyCostOut[];
  unavailable: { strategy: string; reason: string }[];
  assumptions: string[];
  warnings: string[];
  caveat: string;
  interpretation: string;
  observed: ObservedMarket;
}

/** One engine's answer, or the stated reason there is not one. */
export interface OrderBranch<T> {
  branch: string;
  status: ResultStatus;
  results: T | null;
  warnings: AnalyticalWarning[];
  provenance: Provenance;
}

export interface OrderAnalysisResults {
  model_version: string;
  order_analysis_id: string;
  order: {
    portfolio_id: string;
    instrument_id: string;
    canonical_key: string;
    asset_class: string;
    side: string;
    quantity: string;
    signed_quantity: string;
    order_type: string;
    limit_price: string | null;
    multiplier: string;
    currency: string;
    proposed_position_id: string;
  };
  market_state: MarketStateOut;
  branch_status: Record<string, ResultStatus>;
  branches: {
    VALUATION: OrderBranch<Record<string, unknown>>;
    SURFACE: OrderBranch<Record<string, unknown>>;
    EXECUTION: OrderBranch<OrderCostOut>;
    RISK: OrderBranch<{
      book: Record<string, unknown>;
      greeks: { movements: Movement[] };
      value_at_risk: { method: string; movements: Movement[] } | null;
      stress: { scenario: string; movements: Movement[] } | null;
    }>;
    MARGIN: OrderBranch<{
      model: string;
      movements: Movement[];
      disclaimer: string;
    }>;
  };
  counts: { ok: number; failed: number };
  interpretation: string;
}

export interface OrderAnalysisSummary {
  id: string;
  portfolio_id: string;
  instrument_id: string;
  side: string;
  quantity: string;
  order_type: string;
  limit_price: string | null;
  as_of_timestamp: string | null;
  market_state_id: string | null;
  base_currency: string | null;
  status: ResultStatus;
  branches_ok: number;
  branches_failed: number;
  branch_status: Record<string, ResultStatus>;
  created_at: string;
}
