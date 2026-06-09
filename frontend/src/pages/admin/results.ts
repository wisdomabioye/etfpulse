/**
 * Admin action result-display components (#187 split).
 *
 * Barrel re-export so consumers can keep importing from `./results`.
 * Components are split by domain into `results-pipeline` (signal-pipeline
 * ops) and `results-execution` (execution/delivery ops); the shared
 * `ResultLine` helper lives in `results-shared`.
 */

export * from './results-pipeline';
export * from './results-execution';
