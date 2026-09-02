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
