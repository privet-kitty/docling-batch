# Sample PDF for smoke testing

No binary fixture is committed here. For a quick end-to-end test, use any
small PDF you have locally, or grab a public one, e.g.:

```
curl -L -o samples/sample.pdf https://arxiv.org/pdf/1706.03762
```

For testing `--image-export-mode=referenced`, pick a PDF that actually
contains at least one image/figure (the arXiv paper above works), so the
run also exercises the `<name>_artifacts/` image extraction path, not just
the text/markdown path.
