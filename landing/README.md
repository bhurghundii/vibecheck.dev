# Landing page

Static marketing page for vibecheck, deployed on Vercel. No build step: `index.html` is served as is.

## Deploy

Create a Vercel project from this repository and set **Root Directory** to `landing`. Framework preset: Other. Every push to `main` deploys to production, and every PR gets a preview URL.

From the CLI instead:

```sh
cd landing
vercel        # preview
vercel --prod # production
```

## Preview locally

```sh
python -m http.server -d landing 8080
```
