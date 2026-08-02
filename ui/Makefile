.PHONY: install dev build lint test check

install:
	npm install

dev:
	npm run dev

build:
	npm run build

lint:
	npx svelte-check --tsconfig ./tsconfig.json

test:
	npx vitest run

check: lint test
