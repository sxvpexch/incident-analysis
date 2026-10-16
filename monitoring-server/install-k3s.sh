#!/usr/bin/env bash
# Installs single-node k3s + Helm on the monitoring server (Ubuntu). Run once, as a user with sudo.
set -euo pipefail
PRIVATE_IP="${1:?usage: $0 <private-ip-of-this-server>}"
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="v1.36.5+k3s1" \
  INSTALL_K3S_EXEC="server --write-kubeconfig-mode=600 --node-name=monitoring --tls-san=${PRIVATE_IP}" sh -
sudo systemctl enable --now k3s
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown "$USER": ~/.kube/config && chmod 600 ~/.kube/config
grep -q 'KUBECONFIG=' ~/.bashrc || echo 'export KUBECONFIG=$HOME/.kube/config' >> ~/.bashrc
command -v helm >/dev/null || curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
sudo apt-get install -y gettext-base >/dev/null   # envsubst
KUBECONFIG=~/.kube/config kubectl wait --for=condition=Ready node/monitoring --timeout=180s
echo "k3s ready. Run: export KUBECONFIG=~/.kube/config"
