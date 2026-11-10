"""Demo module. Exists only to give vibecheck something to ask about."""


def average_of_evens(numbers):
    # Returns the average of the even numbers in the list, or 0 if there
    # are none. Odd numbers are ignored entirely.
    evens = [n for n in numbers if n % 2 == 0]
    if not evens:
        return 0
    return sum(evens) / len(evens)
