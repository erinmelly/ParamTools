from paramtools import utils


def test_get_leaves():
    t = {
        0: {"value": {0: ["leaf1", "leaf2"]}, 1: {"value": {0: ["leaf3"]}}},
        1: {
            "value": {1: ["leaf4", "leaf5"]},
            2: {"value": {0: ["leaf6", ["leaf7", "leaf8"]]}},
        },
    }

    leaves = utils.get_leaves(t)
    assert leaves == [f"leaf{i}" for i in range(1, 9)]

    leaves = utils.get_leaves([t])
    assert leaves == [f"leaf{i}" for i in range(1, 9)]

    leaves = utils.get_leaves({})
    assert leaves == []

    leaves = utils.get_leaves([])
    assert leaves == []

    leaves = utils.get_leaves("leaf")
    assert leaves == ["leaf"]


def test_ravel():
    a = 1
    assert utils.ravel(a) == 1

    b = [1, 2, 3]
    assert utils.ravel(b) == [1, 2, 3]

    c = [[1], 2, 3]
    assert utils.ravel(c) == [1, 2, 3]

    d = [[1, 2, 3], [4, 5, 6]]
    assert utils.ravel(d) == [1, 2, 3, 4, 5, 6]

    e = [0, [1, 2, 3], 4, [5, 6, 7], 8]
    assert utils.ravel(e) == [0, 1, 2, 3, 4, 5, 6, 7, 8]
