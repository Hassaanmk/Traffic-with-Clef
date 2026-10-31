# Local street-cam watcher on kind.
#   make up       everything from scratch: cluster, KEDA, secrets, images, deploy
#   make deploy   rebuild images and apply changes
#   make status   what is running
#   make grab     run the grabber now instead of waiting for the 5-minute mark
#   make dashboard  open the live results page on http://localhost:8080
#   make down     delete the whole cluster

CLUSTER := streetcam
NS := streetcam
KEDA_VERSION := 2.17.0

.PHONY: up cluster keda secrets images deploy status logs grab db dashboard down

up: cluster keda secrets deploy

cluster:
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER) || kind create cluster --config deploy/kind/kind-config.yaml
	kubectl apply -f deploy/k8s/00-namespace.yaml

keda:
	helm repo add kedacore https://kedacore.github.io/charts --force-update
	helm upgrade --install keda kedacore/keda --version $(KEDA_VERSION) -n keda --create-namespace --wait

secrets:
	@test -f .env || { echo ".env is missing: copy .env.example and fill it in"; exit 1; }
	grep '^CF_' .env | kubectl -n $(NS) create secret generic cloudflare --from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
	@# The Postgres password is made once; changing it later would lock the scorer out of the existing database.
	@kubectl -n $(NS) get secret postgres >/dev/null 2>&1 || kubectl -n $(NS) create secret generic postgres --from-literal=POSTGRES_PASSWORD=$$(openssl rand -hex 16)
	kubectl -n $(NS) create configmap postgres-schema --from-file=db/schema.sql --dry-run=client -o yaml | kubectl apply -f -

images:
	docker build -t streetcam/grabber:dev services/grabber
	docker build -t streetcam/scorer:dev services/scorer
	docker build -t streetcam/dashboard:dev services/dashboard
	kind load docker-image streetcam/grabber:dev streetcam/scorer:dev streetcam/dashboard:dev --name $(CLUSTER)

deploy: images
	kubectl apply -f deploy/k8s/
	kubectl apply -f deploy/keda/
	kubectl -n $(NS) rollout restart deployment/scorer deployment/dashboard

status:
	kubectl -n $(NS) get pods,cronjobs,jobs,scaledobjects

logs:
	kubectl -n $(NS) logs -l app=scorer -f --max-log-requests 5

grab:
	kubectl -n $(NS) create job --from=cronjob/grabber grab-$$(date +%s)

db:
	kubectl -n $(NS) exec -it postgres-0 -- psql -U streetcam -d streetcam

dashboard:
	@echo "Open http://localhost:8080 (Ctrl+C to stop)"
	kubectl -n $(NS) port-forward svc/dashboard 8080:8080

down:
	kind delete cluster --name $(CLUSTER)
