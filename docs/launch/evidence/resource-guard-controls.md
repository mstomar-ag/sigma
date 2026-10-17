# Resource guard controls

Each clause below is covered by the named tests. A "control" is a deliberate break of one clause with the covering
test seen red. The right-hand column says only what was actually done and whether `tools/resource_controls.py` re-runs it.

| Clause | Covering tests | Break seen red |
| --- | --- | --- |
| reserve and share cap | tests/test_resource_guard_limits.py::test_headroom_is_the_smaller_of_free_room_and_share_room | covered by the runner: `tools/resource_controls.py --only reserve-share` (#1797) |
| CPU reserve | tests/test_resource_guard_limits.py::test_cpu_reserve_scales_with_cores | covered by the runner: `tools/resource_controls.py --only cpu-reserve` (#1797) |
| load and pressure level | tests/test_resource_guard_limits.py::test_pressure_two_blocks_goal_but_admits_one_verify_worker | covered by the runner: `tools/resource_controls.py --only pressure-level` (#1797) |
| critical | tests/test_resource_guard_limits.py::test_pressure_four_and_low_available_are_each_critical | covered by the runner: `tools/resource_controls.py --only critical` (#1797) |
| hysteresis (lowest of three, consecutive resume) | tests/test_resource_guard_limits.py::test_lowest_of_three_control, test_resume_median_control, test_consecutive_count_control | the suite's own control tests; covered by the runner: `tools/resource_controls.py --only hysteresis-lowest` `tools/resource_controls.py --only hysteresis-median` `tools/resource_controls.py --only hysteresis-consecutive` (#1797) |
| remainder and stale lease | tests/test_resource_lease.py::test_remainder_clause_control, test_stale_clause_controls | the suite's own control tests; covered by the runner: `tools/resource_controls.py --only remainder` `tools/resource_controls.py --only stale-pid` `tools/resource_controls.py --only stale-ttl` (#1797) |
| floor of one unit | tests/test_resource_lease.py::test_no_observations_admits_exactly_one_unit_then_holds | covered by the runner: `tools/resource_controls.py --only floor-one` (#1797) |
| shed: three consecutive critical samples | tests/test_resource_shed_1602.py::test_one_critical_sample_does_nothing | seen red with the count set to 1 (#1602) `tools/resource_controls.py --only shed-count` and `tools/resource_controls.py --only shed-after-key` re-run it in CI (#1769) |
| shed: off switch | tests/test_resource_shed_1602.py::test_shed_off_does_nothing_even_when_critical | seen red with the switch ignored (#1602) `tools/resource_controls.py --only shed-off` re-runs it in CI (#1769) |
| shed: stop file removed | tests/test_resource_shed_1602.py::test_the_stop_file_is_removed_after_a_shed_and_after_an_exception | seen red with the removal skipped (#1602) `tools/resource_controls.py --only stop-file` re-runs it in CI (#1769) |
| shed: enforce mode only | tests/test_resource_shed_1602.py::test_observe_mode_never_sheds | seen red with the mode clause dropped (#1602) `tools/resource_controls.py --only enforce-only` re-runs it in CI (#1769) |
| learned cost is the measured footprint | tests/test_resource_verify_1601.py::test_learned_cost_is_peak_tree_footprint_over_workers_within_20_percent | seen red with a meter that sums nothing (#1601); covered by the runner: `tools/resource_controls.py --only learned-cost` (#1797) |

`tools/resource_controls.py` applies each break named above as an exact-text patch (`tests/resource_controls/controls.json`) to a temp copy and requires the named test to fail; the CI job `resource-controls` runs it. Every row has at least one patch there, so the runner covers all twelve rows; no row is unpatchable. The runner's own CI run is unmeasured; it was run locally once (#1797), all breaks red.
