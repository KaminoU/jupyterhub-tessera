import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import prettier from 'eslint-config-prettier';

export default tseslint.config(
  {
    ignores: [
      'lib/',
      'node_modules/',
      'frontend/node_modules/',
      'src/tessera/labextension/',
      'docs/build/',
      'eslint.config.mjs',
      'vitest.config.ts'
    ]
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  prettier,
  {
    files: ['frontend/src/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/no-explicit-any': 'error',
      complexity: ['error', 10],
      'no-console': 'error'
    }
  }
);
