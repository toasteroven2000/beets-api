{{/*
Expand the name of the chart.
*/}}
{{- define "beets-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "beets-api.fullname" -}}
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
{{- define "beets-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "beets-api.labels" -}}
helm.sh/chart: {{ include "beets-api.chart" . }}
{{ include "beets-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "beets-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "beets-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}


{{/*
The volumes that are mounted: the Trash only exists when beetsApiConfig.trashDir is set.
*/}}
{{- define "beets-api.volumeNames" -}}
{{- $names := list "config" "music" -}}
{{- if .Values.beetsApiConfig.trashDir -}}
{{- if not (hasPrefix "/" .Values.beetsApiConfig.trashDir) -}}
{{- fail "beetsApiConfig.trashDir must be an absolute path" -}}
{{- end -}}
{{- $names = append $names "trash" -}}
{{- end -}}
{{- join " " $names -}}
{{- end }}

{{/*
BEETSDIR: beetsConfig.dir without a trailing slash, which must be absolute and not the music or Trash volume.
*/}}
{{- define "beets-api.beetsDir" -}}
{{- $dir := trimSuffix "/" .Values.beetsConfig.dir -}}
{{- if not (hasPrefix "/" $dir) -}}
{{- fail "beetsConfig.dir must be an absolute path other than /" -}}
{{- end -}}
{{- if or (eq $dir "/music") (eq $dir (trimSuffix "/" .Values.beetsApiConfig.trashDir)) -}}
{{- fail "beetsConfig.dir must differ from the music directory and beetsApiConfig.trashDir" -}}
{{- end -}}
{{- $dir -}}
{{- end }}

{{/*
Where config.yaml comes from: "existing", "inline", or "volume" (already in /config).
*/}}
{{- define "beets-api.configSource" -}}
{{- if .Values.beetsConfig.existing.name -}}
{{- if not (has .Values.beetsConfig.existing.kind (list "ConfigMap" "Secret")) -}}
{{- fail "beetsConfig.existing.kind must be ConfigMap or Secret" -}}
{{- end -}}
existing
{{- else if .Values.beetsConfig.raw -}}
inline
{{- else -}}
volume
{{- end -}}
{{- end }}

{{/*
The name of the claim behind /config, /music or /trash: the user's own, or one made by this chart.
Called as (list $ "music").
*/}}
{{- define "beets-api.claimName" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
{{- default (printf "%s-%s" (include "beets-api.fullname" $root) $name) (index $root.Values.storage $name).existingClaim -}}
{{- end }}
