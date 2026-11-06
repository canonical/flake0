# Run as root inside spread by the lifecycle task, to prove the markers plugin loads
# there. check.sh looks for it. Not named test_*.py, so `pytest tests/` skips it.
def test_probe():
    pass
