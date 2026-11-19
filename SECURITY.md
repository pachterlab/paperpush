# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue:

1. Go to <https://github.com/pachterlab/paperpush/security/advisories/new>
   (Security tab › *Report a vulnerability*).
2. Describe the problem, the affected component (CLI, MCP server, website,
   agent skills, a venue runner), how to reproduce it, and its impact as you
   see it.

You will get an acknowledgement within **5 business days**. We aim to fix
critical issues within **30 days** and others within **90 days**, and we
will keep you updated along the way. Once a fix ships, we publish a GitHub
security advisory and credit you unless you would rather stay anonymous.

## Scope

In scope:

- the `paperpush` Python package (CLI, MCP server, `paperpush web`),
- the website at <https://paperpush.onrender.com> (source in `paperpush/web/`),
- the agent skills in `skills/`,
- the venue runners in `paperpush/venues/`.

Out of scope: the venues' own submission portals, the third-party services
paperpush calls (report those to the service), and denial of service by
volume against the free website.

Particularly welcome: anything that lets someone read another author's
uploaded files or shared forms, exposes a stored venue password or an API
key, lets a manuscript inject instructions that make paperpush or its agent
skills act against the author, or lets an agent send a submission without
the author.

## Supported versions

Security fixes go into the latest release on PyPI and the website. Please
upgrade (`pip install -U paperpush`) before reporting.

## Safe harbor

We will not take legal action over good-faith research that follows this
policy, avoids other people's data, and does not degrade the website for
others. Use your own uploads and shared forms when testing the website.

## More

How paperpush handles data and security: [security/README.md](security/README.md).
What the website does with uploads: <https://paperpush.onrender.com/privacy>.
