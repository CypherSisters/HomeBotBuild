# Contributing to PuffPAI

Thank you for your interest in contributing to PuffPAI! This document provides guidelines for contributing.

## Start here

You are welcome here, whether you have one sentence to say or a whole
refactor. We built HomeBot with AI coding tools, and we would love people who
write code for a living to read it and tell us what they'd change
([README, "How this was built"](README.md#how-this-was-built)).

**File an issue.** [Open a new issue](https://github.com/CypherSisters/HomeBotBuild/issues/new/choose)
and pick a form: **Bug report** (something broke), **Capability gap** (it
didn't do or understand what you asked), **Idea** (a use case or feature,
emergency preparedness or anything else), or **Code feedback** (you read the
code and have refactors, architecture or test changes in mind). Or open a
blank issue and just write. Security problems go to private vulnerability
reporting instead ([SECURITY.md](SECURITY.md)).

**Send a pull request.** Fork, branch from `main`, keep the change focused,
and add the test that would have caught the bug. Fill in the short pull
request form: what changed, and the test commands you ran with their results
before and after. Read [AGENTS.md](AGENTS.md) first: section 6 lists the
contracts a change must keep and the tests that pin them, section 9 what
never to do.

**Run the tests.** Use the isolated commands in
[AGENTS.md section 5](AGENTS.md#5-testing) (Git Bash, Linux or PowerShell), so
nothing reads your OS keyring, a real API key or real state. The suite has
known failures (the README's Test status paragraph says where they cluster);
compare the failure list before and after your change. A failure that was
already there is not yours. CI runs only a named subset; run that subset too
before you finish.

The rest of this document is the detail.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
- [Development Setup](#development-setup)
- [Coding Standards](#coding-standards)
- [Pull Request Process](#pull-request-process)
- [Community](#community)

---

## Code of Conduct

### Our Pledge

We are committed to providing a welcoming and inspiring community for all. Please be respectful and constructive.

### Our Standards

**Positive behavior:**
- Using welcoming and inclusive language
- Being respectful of differing viewpoints
- Gracefully accepting constructive criticism
- Focusing on what is best for the community

**Unacceptable behavior:**
- Harassment of any kind
- Trolling or insulting comments
- Publishing others' private information
- Other conduct which could reasonably be considered inappropriate

---

## How Can I Contribute?

### Reporting Bugs

Before creating bug reports, please check existing issues. When creating a bug report, include:

- **Clear title** describing the issue
- **Detailed steps** to reproduce
- **Expected behavior** vs actual behavior
- **System information** (OS, Python version, hardware)
- **Screenshots** if applicable
- **Logs** from the console, `<data dir>/logs/puffpai.log`, or `journalctl --user -u homebot-api`

The Bug report form asks for all of this. As a guide:
```markdown
**Environment:**
- OS: Windows 11 / Ubuntu 24.04 / Fedora 41, whichever you are on
- Python: 3.12.3
- Commit: <git rev-parse --short HEAD>
- Hardware: CPU, RAM, GPU/VRAM (the wizard's census prints all of it)

**Steps to Reproduce:**
1. Start PuffPAI
2. Click "New Chat"
3. Send message "test"

**Expected Behavior:**
Response appears in chat

**Actual Behavior:**
Application crashes with error

**Error Message:**
[Paste error here]
```

### Suggesting Features

Feature requests are welcome! Please:

- **Check existing requests** first
- **Explain the use case** clearly
- **Describe the expected behavior**
- **Consider alternatives** you've thought about

### Contributing Code

We welcome pull requests for:

- Bug fixes
- Fixes for the known failing tests
- Refactors and architecture changes, with the tests that prove nothing broke
- New agent tools
- Documentation improvements
- Translations
- Accessibility improvements
- Performance optimizations

---

## Development Setup

### Platforms

**Windows and Linux are the supported targets**, and both are first-class:
`setup.py` classifies for both, `install.ps1` and `install.sh` are maintained
in parallel, and a change that only works on one is not finished. A few
surfaces are Linux-only by nature and say so rather than pretending: Zone 3
(binds port 53, driven by a compose file and systemd), the Pulse volume lever,
and Piper speech-out (no Windows wheel). `docs/INSTALL_WINDOWS.md` has the full
table.

**macOS is not installer-covered.** There is no `install.command`, and
`install.sh` is written for Linux. `src/puffpai/hardware_census.py` does
recognise Darwin, so a manual venv install may well work; patches that keep it
working are welcome, and are untested by us. Do not claim macOS support in a
PR description you have not run on a Mac.

No Jetson is required to contribute. The Jetson AGX Orin appliance is one
deployment (`docs/INSTALL_ORIN.md`), not the development target.

### Prerequisites

- Python 3.10+ (3.10 and 3.12 are what CI runs)
- Git
- Ollama, for anything that actually calls a model. Which model is the wizard's
  answer, not a fixed one: it ranks profiles against your RAM and VRAM. The
  test suite does not need Ollama running.
- Node 20.19+ (or 22.12+), only for dashboard work

There is no RAM floor for working on the code. The floor is a *runtime*
question the wizard answers from your hardware.

### Setup Steps

1. **Fork the repository** on GitHub.

2. **Clone your fork and add upstream**
   ```bash
   git clone https://github.com/<you>/HomeBotBuild.git
   cd HomeBotBuild
   git remote add upstream https://github.com/CypherSisters/HomeBotBuild.git
   ```

3. **Create a virtual environment**

   Linux / macOS:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   python -m pip install -r requirements.txt
   python -m pip install -e ".[dev]"
   ```

   Windows (PowerShell, one command per line, `&&` is not valid in
   PowerShell 5.1):
   ```powershell
   py -3 -m venv venv
   .\venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python -m pip install -e ".[dev]"
   ```

4. **Create a branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

### Running Tests

Always run pytest isolated, with the environment from
[AGENTS.md section 5](AGENTS.md#5-testing) (`PYTHONPATH=src`, the null
keyring, a throwaway `PUFFPAI_API_KEY`, temporary `PUFFPAI_DATA_DIR` and
`PUFFPAI_STATE_DIR`). With that set, in the activated venv:

```bash
python -m pytest         # Windows, Linux or macOS: the whole suite
```

**The suite is large:** well over ten thousand tests, and a single-process run
takes over an hour on the Windows PC we test on. Some of them fail; they are
known, and the README's test status gives the count and says where they
cluster. A few tests depend on the order they run in, so a single run and a
split run can differ by a handful. CI runs only a named subset (the wizard, setup API,
release, config and household tests), not the whole suite. Run a subset while
you work:

```bash
python -m pytest tests/test_config.py                  # one file
python -m pytest tests/test_wizard_*.py                # a family
python -m pytest -k "zone3 and not slow"               # by name
python -m pytest -x -q tests/test_household.py         # stop at the first failure
python -m pytest --cov=src/puffpai tests/test_config.py
```

`pytest.ini` sets `testpaths = tests` and `-v --tb=short`, so bare `pytest`
from the repo root collects everything. Add `-q` when you want the short form.

Tests must be order-independent: run your new file both alone and inside its
family before you push.

### Code Quality

```bash
black src/puffpai          # format (line length 100)
flake8 src/puffpai         # lint
mypy src/puffpai           # type check
```

All three come from the `[dev]` extra. **`mypy` has no configuration file in
this repo**, so it runs with library defaults and is not a gate; treat its
output as advice on the code you touched, not a bar to clear for the whole
tree.

---

## Coding Standards

### Python Style

- Follow **PEP 8** style guide
- Use **Black** for formatting (line length: 100)
- Use **type hints** for function signatures
- Write **docstrings** for all public functions

**Example:**
```python
def calculate_water_needs(
    people: int,
    days: int,
    gallons_per_person_per_day: float = 1.0
) -> float:
    """
    Calculate total water needs for emergency preparedness.
    
    Args:
        people: Number of people
        days: Number of days
        gallons_per_person_per_day: Water consumption rate
        
    Returns:
        Total gallons needed
        
    Example:
        >>> calculate_water_needs(4, 14)
        56.0
    """
    return people * days * gallons_per_person_per_day
```

### Git Commit Messages

Follow conventional commits format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Code style (formatting, no logic change)
- `refactor`: Code restructuring
- `test`: Adding tests
- `chore`: Maintenance tasks

**Examples:**
```
feat(tools): add firewall integration for device blocking

Implements network firewall rules to block specific devices
from internet access while maintaining local network connectivity.

Closes #123
```

```
fix(rag): handle empty search results gracefully

Previously crashed when no results found. Now returns helpful
message suggesting alternative search terms.

Fixes #456
```

### Documentation

- Update README.md for major features
- Add docstrings to all functions
- Include examples in docstrings
- Update relevant docs/ files
- Add comments for complex logic

---

## Pull Request Process

### Before Submitting

- [ ] Code follows style guidelines
- [ ] The tests covering what you changed pass locally, named in the PR
- [ ] Code is formatted (`black`, `flake8`)
- [ ] Documentation is updated
- [ ] Commit messages are clear
- [ ] Branch is up to date with `main`

### PR Description Template

```markdown
## Description
[Clear description of what this PR does]

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Performance improvement

## Testing
[Which tests, on which OS. Name the commands you ran.]

## Screenshots (if applicable)
[Add screenshots]

## Checklist
- [ ] Code follows style guidelines
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] No breaking changes
- [ ] Ran on: [ ] Windows  [ ] Linux  [ ] macOS (untested is fine, say so)

## Related Issues
Closes #[issue number]
```

### Review Process

1. **Automated checks** must pass (GitHub Actions)
2. **Code review** by maintainers
3. **Testing** on multiple platforms if needed
4. **Approval** from at least one maintainer
5. **Merge** by maintainers

### After PR is Merged

```bash
# Sync your fork
git checkout main
git pull upstream main
git push origin main

# Delete your feature branch
git branch -d feature/your-feature-name
git push origin --delete feature/your-feature-name
```

---

## Community

### Communication Channels

- **GitHub Issues**: Bug reports, capability gaps, ideas, code feedback, questions
- **Pull Requests**: Code contributions

### Recognition

Contributors will be:
- Mentioned in release notes
- Credited in project documentation

### Priority Areas

We especially need help with:

1. **Testing on hardware we do not have**: AMD GPUs, Intel Arc, low-RAM
   laptops, unusual distros, voice on Windows. The wizard ranks model profiles
   against a census; a census that misreads your machine is a bug worth a report.
2. **The known failing tests**, and the known exceptions to "one way out" in
   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 9.3
3. **Documentation** improvements and tutorials
4. **Translations** for international users
5. **Accessibility** features
6. **New agent tools**

---

## Questions?

- **General questions**: [open a blank issue](https://github.com/CypherSisters/HomeBotBuild/issues/new) and just ask
- **Bug reports**: [GitHub Issues](https://github.com/CypherSisters/HomeBotBuild/issues)
- **Security issues**: See [SECURITY.md](SECURITY.md): use GitHub private vulnerability reporting, not a public issue or email

---

Thank you for contributing to HomeBot.
