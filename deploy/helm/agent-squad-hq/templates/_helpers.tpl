{{- define "squadhq.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "squadhq.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "squadhq.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "squadhq.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "squadhq.selector" -}}
app.kubernetes.io/name: {{ include "squadhq.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "squadhq.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ default .Chart.AppVersion .Values.image.tag }}
{{- end -}}
{{- end -}}

{{- define "squadhq.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "squadhq.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{- define "squadhq.secretName" -}}
{{ default (printf "%s-secrets" (include "squadhq.fullname" .)) .Values.secrets.existingSecret }}
{{- end -}}

{{- define "squadhq.configMapName" -}}
{{ default (printf "%s-config" (include "squadhq.fullname" .)) .Values.existingConfigMap }}
{{- end -}}

{{/* Environment shared by every component. */}}
{{- define "squadhq.env" -}}
- name: SQUADHQ_HUB_REPO
  value: {{ .Values.config.hubRepo | quote }}
{{- with .Values.config.org }}
- name: SQUADHQ_ORG
  value: {{ . | quote }}
{{- end }}
- name: SQUADHQ_BOT_LOGIN
  value: {{ .Values.config.botLogin | quote }}
- name: SQUADHQ_MODE
  value: {{ .Values.config.mode | quote }}
- name: SQUADHQ_DISPATCH_WORKFLOW
  value: {{ .Values.config.dispatchWorkflow | quote }}
- name: SQUADHQ_DISPATCH_REF
  value: {{ .Values.config.dispatchRef | quote }}
- name: SQUADHQ_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: SQUADHQ_LOG_FORMAT
  value: {{ .Values.config.logFormat | quote }}
- name: SQUADHQ_METRICS_PUBLIC
  value: {{ .Values.config.metricsPublic | quote }}
- name: SQUADHQ_API_DOCS
  value: {{ .Values.config.apiDocs | quote }}
- name: SQUADHQ_MAX_BODY_BYTES
  value: {{ .Values.config.maxBodyBytes | quote }}
- name: SQUADHQ_WEBHOOK_MAX_BODY_BYTES
  value: {{ .Values.config.webhookMaxBodyBytes | quote }}
- name: SQUADHQ_TRUSTED_ASSOCIATIONS
  value: {{ .Values.config.trustedAssociations | default .Values.config.allowedAssociations | quote }}
- name: SQUADHQ_REQUIRED_CHECKS
  value: {{ .Values.config.requiredChecks | quote }}
- name: SQUADHQ_REQUIRED_CHECKS_APP
  value: {{ .Values.config.requiredChecksApp | quote }}
- name: SQUADHQ_REQUIRED_CHECKS_WORKFLOW
  value: {{ .Values.config.requiredChecksWorkflow | quote }}
- name: SQUADHQ_ALLOW_FORK_MERGES
  value: {{ .Values.config.allowForkMerges | quote }}
- name: SQUADHQ_TRUSTED_ACTORS
  value: {{ .Values.config.trustedActors | quote }}
- name: SQUADHQ_TRELLO_CALLBACK_URL
  value: {{ .Values.config.trelloCallbackUrl | quote }}
- name: SQUADHQ_OTEL_ENABLED
  value: {{ .Values.config.otelEnabled | quote }}
- name: SQUADHQ_CONFIG
  value: /etc/squadhq/squadhq.yml
- name: SQUADHQ_PORT
  value: "8080"
{{- range $k, $v := .Values.config.extraEnv }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- $secret := include "squadhq.secretName" . }}
{{- range .Values.secrets.keys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ . }}
      optional: {{ ne . "SQUADHQ_DB_URL" }}
{{- end }}
{{- end -}}

{{/* Pod spec shared by api / worker / scheduler / migrate. Args: root, component, cmd, extra. */}}
{{- define "squadhq.podSpec" -}}
{{- $v := .root.Values -}}
serviceAccountName: {{ include "squadhq.serviceAccountName" .root }}
automountServiceAccountToken: {{ $v.serviceAccount.automountToken }}
{{- with $v.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml $v.podSecurityContext | nindent 2 }}
containers:
  - name: {{ .component }}
    image: {{ include "squadhq.image" .root }}
    imagePullPolicy: {{ $v.image.pullPolicy }}
    args: [{{ .cmd | quote }}]
    securityContext:
      {{- toYaml $v.securityContext | nindent 6 }}
    env:
      {{- include "squadhq.env" .root | nindent 6 }}
    {{- if .ports }}
    ports:
      - name: http
        containerPort: 8080
        protocol: TCP
    readinessProbe:
      httpGet: {path: /readyz, port: http}
      periodSeconds: 10
    livenessProbe:
      httpGet: {path: /healthz, port: http}
      periodSeconds: 20
      failureThreshold: 3
    startupProbe:
      httpGet: {path: /healthz, port: http}
      periodSeconds: 3
      failureThreshold: 30
    {{- end }}
    {{- with .resources }}
    resources:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    volumeMounts:
      - name: config
        mountPath: /etc/squadhq
        readOnly: true
      - name: tmp
        mountPath: /tmp
      - name: data
        mountPath: /var/lib/squadhq
volumes:
  - name: config
    configMap:
      name: {{ include "squadhq.configMapName" .root }}
  - name: tmp
    emptyDir:
      medium: Memory
      sizeLimit: 256Mi
  - name: data
    emptyDir:
      sizeLimit: 1Gi
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
