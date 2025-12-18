{{/*
Expand the name of the chart.
*/}}
{{- define "vomeet.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "vomeet.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "vomeet.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "vomeet.labels" -}}
helm.sh/chart: {{ include "vomeet.chart" . }}
{{ include "vomeet.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "vomeet.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vomeet.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service-specific labels
*/}}
{{- define "vomeet.serviceLabels" -}}
{{ include "vomeet.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Service-specific selector labels
*/}}
{{- define "vomeet.serviceSelectorLabels" -}}
{{ include "vomeet.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Get image for a service
*/}}
{{- define "vomeet.image" -}}
{{- $svcConfig := index .root.Values .service -}}
{{- $registry := .root.Values.vomeetImages.registry -}}
{{- $prefix := .root.Values.vomeetImages.prefix -}}
{{- $tag := default .root.Values.global.imageTag $svcConfig.image.tag -}}
{{- $repo := default (printf "%s-%s" $prefix .service) $svcConfig.image.repository -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end }}

{{/*
Database helpers (CNPG)
*/}}
{{- define "vomeet.databaseHost" -}}
{{- printf "%s-rw" "vomeet-cnpg" -}}
{{- end }}

{{- define "vomeet.databasePort" -}}
5432
{{- end }}

{{- define "vomeet.databaseName" -}}
{{- .Values.postgresql.database -}}
{{- end }}

{{- define "vomeet.databaseUser" -}}
{{- .Values.postgresql.username -}}
{{- end }}

{{/*
Redis helpers (Dragonfly)
*/}}
{{- define "vomeet.redisHost" -}}
{{- "dragonfly" -}}
{{- end }}

{{- define "vomeet.redisPort" -}}
6379
{{- end }}

{{- define "vomeet.redisUrl" -}}
{{- printf "redis://%s:%s/0" (include "vomeet.redisHost" .) (include "vomeet.redisPort" .) -}}
{{- end }}
