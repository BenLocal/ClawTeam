# ghostty-web 0.4.0

Browser distribution from https://github.com/coder/ghostty-web, licensed under MIT.
These files are served locally so terminals work without a CDN or internet access.
The ES module includes the terminal's WebAssembly binary.

Source package: `ghostty-web@0.4.0` from npm.
Tarball SHA-1: `902ff2fc042392b9361410b458875d38fbb8099f`.

To update, run `npm pack ghostty-web@<version>`, extract the package, and copy
`dist/ghostty-web.js`, its `dist/__vite-browser-external-*.js` helper, and `LICENSE`
here. Update the asset allowlist in `board/server.py` if the helper's name changes.
