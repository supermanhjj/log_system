{{- define "ruilog-collector.fullname" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ruilog-collector.secretName" -}}
{{- if .Values.collector.existingSecret -}}
{{- .Values.collector.existingSecret -}}
{{- else -}}
{{- printf "%s-key" (include "ruilog-collector.fullname" .) -}}
{{- end -}}
{{- end -}}
