import {defineConfig} from "vite";
import vue from "@vitejs/plugin-vue";
import federation from "@originjs/vite-plugin-federation";
import {resolve} from "node:path";
import {fileURLToPath} from "node:url";

const outputDir = resolve(
    fileURLToPath(new URL(".", import.meta.url)),
    "../../plugins.v2/pansearch/dist/assets",
);

export default defineConfig({
    base: "./",
    plugins: [
        vue(),
        federation({
            name: "pansearch",
            filename: "remoteEntry.js",
            exposes: {
                "./Page": "./src/components/Page.vue",
                "./Config": "./src/components/Config.vue",
                "./Dashboard": "./src/components/Dashboard.vue",
                "./AppPage": "./src/components/AppPage.vue",
                "./AppPageResource": "./src/components/AppPageResource.vue",
            },
            shared: {
                vue: {requiredVersion: false, generate: false},
                vuetify: {requiredVersion: false, generate: false, singleton: true},
                "vuetify/styles": {
                    requiredVersion: false,
                    generate: false,
                    singleton: true,
                },
            },
            format: "esm",
        }),
    ],
    build: {
        target: "esnext",
        minify: "esbuild",
        cssCodeSplit: true,
        emptyOutDir: true,
        outDir: outputDir,
        assetsDir: "",
    },
});
