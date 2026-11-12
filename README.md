# Personal job board

Checks the career pages in `config.yaml` every day, keeps the links that match
your keywords, and shows them on a simple web page. New postings are marked,
and a strip at the top shows which sites worked on the last run.

## Set it up (about 10 minutes, free)

1. Create a new repository on GitHub and upload everything in this folder.
2. **Settings > Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`.
3. **Settings > Actions > General > Workflow permissions**: choose "Read and write permissions".
4. **Actions** tab > "Scrape jobs" > **Run workflow**. The first run takes a few minutes.
5. Your board is at `https://<your-username>.github.io/<repo-name>/`.

After that it runs by itself every day at 06:00 UTC. Change the time in
`.github/workflows/scrape.yml` (the `cron` line).

Pages from a public repo are public. If you don't want others to see your
list, make the repo private (GitHub Pages for private repos needs a paid plan)
or run it on your own computer instead (below).

## Run it on your own computer

```
pip install -r requirements.txt
python scraper.py
python -m http.server -d docs      # then open http://localhost:8000
```

Sites marked `render: js` need a real browser:
`pip install playwright && playwright install chromium`.

## Change what it looks for

Everything is in `config.yaml`: keywords, words to ignore, and the sites.
Keywords ignore case, accents, apostrophes and dashes, and allow plural and
feminine endings ("analyste" also matches "analystes", "chargee"). A keyword
made of several words must appear as that exact phrase. `geopolit*` matches any
word starting with those letters.

Internship-related words (stage, stagiaire, intern, trainee, alternance...) are in
the `exclude` list, so those postings never show up. Remove a word there if you
change your mind. Very general keywords such as "Sovereignty" or "Latin America"
can also match news or article links on some sites. If the board gets noisy,
delete the broad keywords and keep the job-title ones.

Per-site options:

| Option | Use it when |
| --- | --- |
| `render: js` | the page shows no jobs because it builds them with JavaScript (status "empty") |
| `take_all: true` | the site only lists jobs you want anyway, so keywords aren't needed |
| `match_context: true` | the link text is vague but the text around it contains your keyword |
| `ignore_robots: true` | only for sites that explicitly allow automated access |

## Reading the Source status

- **ok**: page read. Zero matches can just mean nothing is open.
- **empty**: the page came back with almost no content. Try `render: js`.
- **blocked**: the site's robots.txt forbids automated access, so the scraper skips it. Check these by hand or use the site's own job alerts.
- **error**: the page didn't load. If it keeps failing, the site may block automated visits.

Postings that vanish from a page that loaded fine are marked "no longer listed".
If a page fails to load, its postings are left alone rather than marked closed.

## Limits worth knowing

- It finds *links* whose text matches your keywords. It doesn't read the full
  job description, deadlines or locations.
- Listings split over several pages need one `sites` entry per page.
- Sites that require a login (LinkedIn, most UN portals) can't be scraped. Use their email alerts.
- Check each site's terms of use before scraping it regularly.
