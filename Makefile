# ---------------------------------------------------------------------------
# serverless-events-observability — validate / test / build / package / publish
# to the AWS Serverless Application Repository.
#
# This repository is the product's source. Consumers deploy a published SAR
# version, or build from a pinned tag of this repository; they never build from
# main. `make deploy` here is for testing the artifact in your own account.
#
# Quick start:
#   make check                                 # every gate that needs no AWS
#   make release S3_BUCKET=my-sar-artifacts    # gates -> build -> package -> leak-check -> publish
#
# Auth: export AWS_PROFILE, or set AWS_VAULT=<profile> to wrap every AWS command
# in `aws-vault exec`:
#   make package AWS_VAULT=my-profile S3_BUCKET=...
# ---------------------------------------------------------------------------

APP_NAME   := serverless-events-observability
REGION     ?= us-east-1
STACK_NAME ?= events-observability-test
S3_BUCKET  ?=
TEMPLATE   := template.yaml
PACKAGED   := packaged.yaml
BUILT_TEMPLATE := .aws-sam/build/template.yaml
PYTHON     ?= python3

# Only when given on the command line. aws-vault itself exports AWS_VAULT inside
# the shells it opens, and wrapping again from there fails with "running in an
# existing aws-vault subshell" -- where the credentials are already present.
ifeq ($(origin AWS_VAULT),command line)
RUN := aws-vault exec $(AWS_VAULT) --
else
RUN :=
endif

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Vars: REGION=$(REGION) STACK_NAME=$(STACK_NAME) S3_BUCKET=$(S3_BUCKET) AWS_VAULT=$(if $(RUN),$(AWS_VAULT),)"

guard-%:
	@if [ -z "$($*)" ]; then \
		echo "ERROR: '$*' is required, e.g. make $(MAKECMDGOALS) $*=<value>"; \
		exit 1; \
	fi

# ---------------------------------------------------------------------------
# Validate / test — none of this needs AWS credentials
# ---------------------------------------------------------------------------
.PHONY: validate
validate: ## Lint & validate the SAM template
	$(RUN) sam validate --lint --region $(REGION)

# Two lanes, and the difference is the ENVIRONMENT, not the command. Running
# the same `pytest tests/ -q` twice in one shell runs the same lane twice and
# reports it as two.
#
# Lane 1 is only meaningful when opensearch-py, boto3 and requests-aws4auth are
# genuinely absent: that is what proves the shaping half stands alone, and the
# search API tests skip. Lane 2 needs them present, or the search API ships
# untested -- the shape of mistake that once put a dependency-less version into
# SAR. So each target checks the environment it claims to be testing. CI runs
# both, in that order, in one job: see .github/workflows/ci.yml.
.PHONY: test
test: ## Run the unit tests (seam lane: requires the delivery deps to be ABSENT)
	@if $(PYTHON) -c 'import opensearchpy' >/dev/null 2>&1; then \
		echo "NOTE: opensearch-py is installed, so this is not the seam lane."; \
		echo "      The standalone-shaping test proves nothing here. Use a clean"; \
		echo "      venv with 'pip install -r tests/requirements.txt' for lane 1,"; \
		echo "      or just run 'make test-full'."; \
	fi
	$(PYTHON) -m pytest tests/ -q

.PHONY: test-full
test-full: ## Run the unit tests with the delivery deps, so the search API is covered
	@$(PYTHON) -c 'import opensearchpy, boto3, requests_aws4auth' 2>/dev/null || { \
		echo "ERROR: the delivery dependencies are not installed, so the search"; \
		echo "       API tests would silently skip and this lane would pass"; \
		echo "       without testing the thing it exists to test."; \
		echo "       Run: pip install -r tests/requirements-full.txt"; \
		exit 1; \
	}
	$(PYTHON) -m pytest tests/ -q

.PHONY: leak-check
leak-check: ## Fail if anything internal is in the tree (see scripts/leak-check.py)
	$(PYTHON) scripts/leak-check.py

.PHONY: check-metadata
check-metadata: ## Fail on SAR metadata sam publish would reject (description length etc.)
	$(PYTHON) scripts/check-sar-metadata.py

.PHONY: check
check: validate test-full leak-check check-metadata ## Every gate that needs no AWS (full test lane)

# ---------------------------------------------------------------------------
# Publish to SAR
# ---------------------------------------------------------------------------
.PHONY: build
build: ## Build with a container (needs Docker; builds Lambda-native wheels)
	# In a container so the compiled dependencies (pydantic-core, via
	# powertools) match the declared runtime rather than whatever interpreter is
	# on PATH. A mismatch fails at cold start, not at build time.
	$(RUN) sam build --use-container

.PHONY: create-bucket
create-bucket: guard-S3_BUCKET ## Create the SAR artifact bucket + grant SAR read access
	$(RUN) aws s3 mb s3://$(S3_BUCKET) --region $(REGION) || true
	@ACCOUNT_ID=$$($(RUN) aws sts get-caller-identity --query Account --output text); \
	echo "Applying SAR read policy for the current account..."; \
	$(RUN) aws s3api put-bucket-policy --bucket $(S3_BUCKET) --policy \
	  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"serverlessrepo.amazonaws.com\"},\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$(S3_BUCKET)/*\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$$ACCOUNT_ID\"}}}]}"

.PHONY: check-build
check-build: ## Fail if the built artifact is missing its dependencies
	$(PYTHON) scripts/check-build.py

.PHONY: package
package: guard-S3_BUCKET check-build ## Upload code artifacts to S3 and emit packaged.yaml
	# BUILT_TEMPLATE, not TEMPLATE. `sam build` installs dependencies under
	# .aws-sam/build/ and writes a template whose CodeUri points at them;
	# packaging the source template instead uploads app/ verbatim with no
	# dependencies, which once shipped an artifact that could not import its
	# own handler. Every other gate passed.
	$(RUN) sam package --template-file $(BUILT_TEMPLATE) --output-template-file $(PACKAGED) \
		--s3-bucket $(S3_BUCKET) --region $(REGION)

.PHONY: publish
publish: ## Publish packaged.yaml to the Serverless Application Repository
	$(RUN) sam publish --template $(PACKAGED) --region $(REGION)
	@echo ""
	@echo "Published. SemanticVersion is IMMUTABLE — bump it in $(TEMPLATE) before"
	@echo "the next publish; you cannot overwrite what just went out. A SAR"
	@echo "application exists only in the region it was published to: publish once"
	@echo "per region that deploys it."

# Refuses unless I_UNDERSTAND_THIS_IS_PUBLIC=yes. Making a SAR application public
# is effectively permanent, and a public application's ARN carries the
# PUBLISHING ACCOUNT ID -- through a channel no leak gate can scan. Publish from
# an account whose id you are content to make world-readable, or share the
# application with specific accounts instead.
.PHONY: make-public
make-public: guard-APPLICATION_ID ## Share the published app with EVERYONE (see the warning in this file)
	@if [ "$(I_UNDERSTAND_THIS_IS_PUBLIC)" != "yes" ]; then \
		echo "REFUSING. Making a SAR application public is effectively permanent,"; \
		echo "and its ARN carries the publishing account id."; \
		echo ""; \
		echo "If that is intended: make make-public APPLICATION_ID=<arn> I_UNDERSTAND_THIS_IS_PUBLIC=yes"; \
		exit 1; \
	fi
	$(RUN) aws serverlessrepo put-application-policy \
		--application-id $(APPLICATION_ID) \
		--region $(REGION) \
		--statements Principals='*',Actions=Deploy

.PHONY: release
release: check build check-build package ## Gates, build, package, leak-check the package, publish
	# Again, with packaged.yaml now in the tree: `sam package` rewrites CodeUri
	# and is the last chance to catch something that only appears after build.
	$(PYTHON) scripts/leak-check.py
	$(MAKE) publish

# ---------------------------------------------------------------------------
# Test deploy into your own account — NOT the SAR listing
# ---------------------------------------------------------------------------
.PHONY: deploy
deploy: build ## Guided deploy into your account for testing (prompts for params)
	# NAMED_IAM, not IAM: FirehoseDeliveryRole and EventsToFirehoseRole carry
	# explicit RoleNames, because the delivery role's ARN gets written into an
	# OpenSearch access policy in another account and a CloudFormation-hashed
	# name would silently invalidate that grant on every replacement.
	# NAMED_IAM and AUTO_EXPAND, and deliberately NOT CAPABILITY_RESOURCE_POLICY:
	# this path goes through CloudFormation, whose enum rejects it. The
	# serverlessrepo API is the opposite and requires it. See README.md.
	$(RUN) sam deploy --guided --stack-name $(STACK_NAME) --region $(REGION) \
		--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND

.PHONY: outputs
outputs: ## Show deployed stack outputs
	$(RUN) aws cloudformation describe-stacks --stack-name $(STACK_NAME) \
		--region $(REGION) --query 'Stacks[0].Outputs' --output table

.PHONY: logs-forwarder
logs-forwarder: ## Tail the OpenSearch forwarder logs
	$(RUN) sam logs --stack-name $(STACK_NAME) --name OpenSearchForwarderFunction \
		--region $(REGION) --tail

.PHONY: logs-digest
logs-digest: ## Tail the alert digest logs
	$(RUN) sam logs --stack-name $(STACK_NAME) --name AlertDigestFunction \
		--region $(REGION) --tail

.PHONY: destroy
destroy: ## Delete the test stack from your account
	$(RUN) sam delete --stack-name $(STACK_NAME) --region $(REGION)

.PHONY: clean
clean: ## Remove build artifacts (.aws-sam, packaged.yaml)
	rm -rf .aws-sam $(PACKAGED)
