# Resource guard controls

Each clause below is covered by the named tests. A "control" is a deliberate break of one clause with the covering
test seen red. The right-hand column says only what was actually done; the clauses marked "not run" have a covering
test but no recorded break in this change.

| Clause | Covering tests | Break seen red |
| --- | --- | --- |
| reserve and share cap | tests/test_resource_guard_limits.py::test_headroom_is_the_smaller_of_free_room_and_share_room | not run |
| CPU reserve | tests/test_resource_guard_limits.py::test_cpu_reserve_scales_with_cores | not run |
| load and pressure level | tests/test_resource_guard_limits.py::test_pressure_two_blocks_goal_but_admits_one_verify_worker | not run |
| critical | tests/test_resource_guard_limits.py::test_pressure_four_and_low_available_are_each_critical | not run |
| hysteresis (lowest of three, consecutive resume) | tests/test_resource_guard_limits.py::test_lowest_of_three_control, test_resume_median_control, test_consecutive_count_control | control tests exist in the suite |
| remainder and stale lease | tests/test_resource_lease.py::test_remainder_clause_control, test_stale_clause_controls | control tests exist in the suite |
| floor of one unit | tests/test_resource_lease.py::test_no_observations_admits_exactly_one_unit_then_holds | not run |
| shed: three consecutive critical samples | tests/test_resource_shed_1602.py::test_one_critical_sample_does_nothing | seen red with the count set to 1 (#1602) |
| shed: off switch | tests/test_resource_shed_1602.py::test_shed_off_does_nothing_even_when_critical | seen red with the switch ignored (#1602) |
| shed: stop file removed | tests/test_resource_shed_1602.py::test_the_stop_file_is_removed_after_a_shed_and_after_an_exception | seen red with the removal skipped (#1602) |
| shed: enforce mode only | tests/test_resource_shed_1602.py::test_observe_mode_never_sheds | seen red with the mode clause dropped (#1602) |
| learned cost is the measured footprint | tests/test_resource_verify_1601.py::test_learned_cost_is_peak_tree_footprint_over_workers_within_20_percent | seen red with a meter that sums nothing (#1601) |

An automated runner that applies each break as a patch in CI is not built yet.
