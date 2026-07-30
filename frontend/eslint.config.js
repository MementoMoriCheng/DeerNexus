import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import tseslint from "typescript-eslint";

// eslint-config-next v16 ships a native flat config (`export = Linter.Config[]`),
// so we can spread it directly — no more @eslint/eslintrc FlatCompat compat layer
// (which crashed on v16's circular-reference react plugin under JSON serialization).
export default tseslint.config(
  {
    ignores: [
      ".next",
      "playwright-report",
      "test-results",
      "src/components/ui/**",
      "src/components/ai-elements/**",
      "*.js",
    ],
  },
  ...nextCoreWebVitals,
  {
    files: ["**/*.ts", "**/*.tsx"],
    extends: [
      ...tseslint.configs.recommended,
      ...tseslint.configs.recommendedTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
    ],
    rules: {
      "@next/next/no-img-element": "off",
      // eslint-config-next v16 turns on React Compiler-derived hooks rules
      // (`set-state-in-effect`, `refs`, `immutability`). They flag ~40 existing,
      // intentional patterns (setState in effects, reading refs during render)
      // across the app. Migrating them is a separate React-refactor effort, not
      // part of the flat-config migration, so they are disabled here to keep the
      // v15→v16 upgrade behavior-neutral. `exhaustive-deps` stays on (warn).
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/refs": "off",
      "react-hooks/immutability": "off",
      "@typescript-eslint/array-type": "off",
      "@typescript-eslint/consistent-type-definitions": "off",
      "@typescript-eslint/consistent-type-imports": [
        "warn",
        { prefer: "type-imports", fixStyle: "inline-type-imports" },
      ],
      "@typescript-eslint/no-unused-vars": [
        "warn",
        // `_`-prefix marks intentionally-unused bindings. Covers both function
        // params (argsIgnorePattern) and local bindings such as a destructured
        // prop that must be stripped before spreading the rest (varsIgnorePattern),
        // e.g. `const { node: _node, ...rest } = props` to drop streamdown's hast
        // `node` before forwarding `rest` to a native element.
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/no-empty-object-type": "off",
      "@typescript-eslint/no-misused-promises": [
        "error",
        { checksVoidReturn: { attributes: false } },
      ],
      "@typescript-eslint/no-redundant-type-constituents": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-call": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-return": "off",
      "import/order": [
        "error",
        {
          distinctGroup: false,
          groups: [
            "builtin",
            "external",
            "internal",
            "parent",
            "sibling",
            "index",
            "object",
          ],
          pathGroups: [
            {
              pattern: "@/**",
              group: "internal",
            },
            {
              pattern: "./**.css",
              group: "object",
            },
            {
              pattern: "**.md",
              group: "object",
            },
          ],
          "newlines-between": "always",
          alphabetize: {
            order: "asc",
            caseInsensitive: true,
          },
        },
      ],
    },
  },
  {
    linterOptions: {
      reportUnusedDisableDirectives: true,
    },
    languageOptions: {
      parserOptions: {
        projectService: true,
      },
    },
  },
);
