from app.evaluation.generators import generate_cases
from app.evaluation.metrics import exact_cell_accuracy


def test_seeded_generator_is_reproducible_and_has_100_cases():
    first = generate_cases(100, seed=19)
    second = generate_cases(100, seed=19)
    assert len(first) == 100
    assert [case.family for case in first] == [case.family for case in second]
    assert first[0].frame.equals(second[0].frame)


def test_exact_match_metric():
    case = generate_cases(1)[0]
    assert exact_cell_accuracy(case.expected, case.expected) == 1.0
