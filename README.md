# vibecheck - quizzes your team on their AI's code before it merges

https://github.com/user-attachments/assets/497f427e-182e-4446-ac0c-ed6f1ad8ea28

**vibecheck is a self-hosted merge gate for GitHub pull requests. Before a PR can merge, the person who opened it has to show they understand their own change.**

This is self-hosted. You will need to host the Docker container plus a Postgres database, on your own server or cloud account. It is BYOK: an Anthropic API key, or [Amazon Bedrock](https://bhurghundii.com/vibecheck.dev/configuration/#amazon-bedrock) in your own AWS account

## How it works

You run vibecheck on your own server (it is [self-hosted](https://bhurghundii.com/vibecheck.dev/quickstart/), with a Docker image on `ghcr.io/bhurghundii/vibecheck.dev`) and add one workflow file to your repository. 

After that, when someone opens a pull request:

1. vibecheck reads the code change and generates a few questions about it. It then comments on the PR with a link to the questions.
2. The author gets the link and answers them in plain English. They get one question at a time about different parts of their code. However, the first question is always *"How does this change work on a high level?"*
3. An AI grader marks each answer against the actual change. You can tune how much detail warrants a pass here.
4. If the author passes the test, they demonstrate the minimum understanding of their work. The check goes green and the PR can merge. If not, the merge stays blocked. 

## Who is it for, and what do you get?

- **Reviewers and maintainers.** Tired of getting spammed with massive PRs from people who cannot explain them? With this, any author will getchecked on the basics before you spend an hour reading.
- **Engineering teams and leads.** Confidence that your team understands every change they ship, even when AI wrote most of it. Use AI to write deliver quality; vibecheck keeps a human in the loop who knows what it does.
- **Open source projects.** A cheap filter for drive-by, AI-generated pull requests that nobody has actually read.
- **Authors.** Unsure of what your AI has actually done for you? Get your understanding checked before someone else does.

## What problem is this solving?

Thanks to AI, it is far easier to generate code that people just don't understand. The burden of understanding therefore falls on the reviewers. When faced with tons of massive PRs that nobody has the energy to read them closely, the result is burnout and rubber-stamping: "LGTM, CI is green." Then you are shipping code that nobody understands. For business critical software, this is simply unacceptable.

Just to be clear, vibecheck does not review the code for you. **It just checks that the author understands it. **

## Features (so far)

- **An exam sheet for every PR.** vibecheck reads the diff once to form its own blind interpretation, then writes a short sheet: the fixed high-level question plus scoped questions, each anchored to a specific part of the diff. The author sees one question per screen, next to just the slice of the diff it is about.
- **Sized to the change.** One scoped question per 15 changed lines, up to `EXAM_QUESTION_COUNT`. A one-line PR gets one question, a 1,000-line monstrosity gets picked apart section by section, and nobody is quizzed on git headers.
- **Graded on submit, leniently.** Claude marks each answer against the blind interpretation. A terse but correct answer passes; contradicting the diff fails. Wrong answers can be retried, then vibecheck reveals the answer and moves on. "Explain it for me" gives a plain-language walkthrough of a section with no effect on the mark, and "Skip" gives up on a question.
- **No understanding? No merge.** The session passes once `PASSINGMARKS` screens are correct and fails as soon as that is out of reach. vibecheck reports it as a `vibecheck/verdict` commit status; require it in branch protection and the PR cannot merge until the author passes.
- **Good development practices check.** Oversized PRs and diffs are rejected before any question is asked. _More coming soon._
- **Local first.** Self-hosted only: one Docker Compose command and it's running, and it works in enterprise environments. BYOK of course.
- **No GitHub App, no OAuth, no webhooks.** It's a FastAPI service that a GitHub Action talks to over a bearer token. Nothing to install on GitHub beyond a workflow file. Just keep the vibecheck token a secret.
- **Sarcastic mode.** Use vibecheck the way that God intended: a grumpy senior will brutally tear your understanding. I used to have a tech lead who did this. I miss them.

## Need a demo?

See it in action on real PRs:

- [Demo 1: Pass a PR](https://github.com/bhurghundii/vibecheck.dev/pull/53): answer well and `vibecheck/verdict` goes green.
- [Demo 2: Fail a PR](https://github.com/bhurghundii/vibecheck.dev/pull/54): answer badly and the merge stays blocked.

## Contributing

I am well aware of the irony of having vibe slop to deal with the problem of vibe slop. 

I **encourage AI generated PRs** over human ones. Why? Because the entire point is to help devs integrate vibe coding more safely. 

I still think humans > AI any day though. 

If you have any issues, get in touch!

## Documentation

Full docs live at **https://bhurghundii.com/vibecheck.dev/** (source in [`docs/`](docs/)):

- [Quickstart](https://bhurghundii.com/vibecheck.dev/quickstart/): self-host it with Docker Compose
- [How it works](https://bhurghundii.com/vibecheck.dev/how-it-works/)
- [Wire it into a repo](https://bhurghundii.com/vibecheck.dev/wire-it-up/) and [block PRs until they pass](https://bhurghundii.com/vibecheck.dev/blocking-prs/)
- [Configuration](https://bhurghundii.com/vibecheck.dev/configuration/)
- [Deploying to Railway](https://bhurghundii.com/vibecheck.dev/deploy-railway/) and the [pre-launch security checklist](https://bhurghundii.com/vibecheck.dev/security/)
- [Local development](https://bhurghundii.com/vibecheck.dev/development/) and [limitations](https://bhurghundii.com/vibecheck.dev/limitations/)

## License
MIT

__I heard you liked vibe slop, so I made vibe slop for your vibe slop. Dedicated to you know who.__ 
