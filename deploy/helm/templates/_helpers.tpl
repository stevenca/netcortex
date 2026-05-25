{{/*
Expand the name of the chart.
*/}}
{{- define "netcortex.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "netcortex.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label
*/}}
{{- define "netcortex.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "netcortex.labels" -}}
helm.sh/chart: {{ include "netcortex.chart" . }}
{{ include "netcortex.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "netcortex.selectorLabels" -}}
app.kubernetes.io/name: {{ include "netcortex.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name
*/}}
{{- define "netcortex.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "netcortex.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Bootstrap secret name — prefers existingSecret over chart-managed secret.
*/}}
{{- define "netcortex.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- include "netcortex.fullname" . }}-bootstrap
{{- end }}
{{- end }}

{{/*
OpenShift-safe container security context.
Does NOT set runAsUser — OCP restricted SCC assigns the UID from the
namespace range; the Dockerfile's GID-0 chown keeps /app/data writable.
*/}}
{{- define "netcortex.containerSecurityContext" -}}
runAsNonRoot: {{ .Values.securityContext.runAsNonRoot }}
allowPrivilegeEscalation: {{ .Values.securityContext.allowPrivilegeEscalation }}
capabilities:
  drop: {{ toJson .Values.securityContext.capabilities.drop }}
{{- if .Values.securityContext.seccompProfile }}
seccompProfile:
  type: {{ .Values.securityContext.seccompProfile.type }}
{{- end }}
{{- end }}

{{/*
Redis URL for peer containers.
*/}}
{{- define "netcortex.redisUrl" -}}
redis://{{ include "netcortex.fullname" . }}-redis:6379/0
{{- end }}

{{/*
Neo4j bolt URI for peer containers.
*/}}
{{- define "netcortex.neo4jUri" -}}
bolt://{{ include "netcortex.fullname" . }}-neo4j:7687
{{- end }}
