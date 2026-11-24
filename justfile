set positional-arguments

# List recipes
[private]
default:
    @just --list

# Install the tools the checks use
setup:
    brew install actionlint shellcheck cryptography

# Run the Python and shell script tests
test:
    python3 -m unittest discover -s tests -v

# Lint the action definitions and shell scripts
lint:
    actionlint
    shellcheck scripts/*.sh

# Full CI gate: lint and test
check: lint test
