"""Demo module. Exists only to give vibecheck something to ask about."""


def marble_machine(drops):
    # Returns how many marbles end up in the left bucket after the given
    # number of drops. Every drop sends a marble right, except each fourth
    # drop, which sends two marbles left, and the left bucket is emptied
    # whenever it holds more marbles than there have been drops.
    left = 0
    for drop in range(1, drops + 1):
        if drop % 4 == 0:
            left += 2
        if left > drop:
            left = 0
    return left
