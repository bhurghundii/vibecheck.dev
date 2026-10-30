# vibecheck

Quizzes your team on their AI's code before it merges.

**Vibecheck is a merge gate that checks the author of the PR understands the change they are putting in.**

vibecheck sits in your PR pipeline as a GitHub Action. Before a PR can merge, it walks whoever opened it through their own diff one section at a time, asking a question about each, and blocks the merge until they answer enough of them well enough to convince an LLM grader.

## Where to start

- [Quickstart](quickstart.md): run it locally in one Docker Compose command.
- [How it works](how-it-works.md): what happens from PR open to verdict.
- [Wire it into a repo](wire-it-up.md): add the GitHub Action.
- [Blocking a PR](blocking-prs.md): make the verdict a required check.
- [Configuration](configuration.md): every environment variable.
- [Before you make it public](security.md): the checklist for a public deployment.
