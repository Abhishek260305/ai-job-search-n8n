/// <reference types="vite/client" />

/** The version in package.json, set by vite.config.ts at build time. */
declare const __APP_VERSION__: string

interface ImportMetaEnv {
  /** The repository's web address (https://github.com/<you>/<repo>), for the docs link in the footer. Optional. */
  readonly VITE_REPO_URL?: string
}
