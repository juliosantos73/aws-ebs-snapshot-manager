# aws-ebs-snapshot-manager

🌐 [English](README.md) | [Português](README.pt-BR.md)

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![AWS Lambda](https://img.shields.io/badge/AWS-Lambda-orange?logo=amazon-aws)

Uma função AWS Lambda que **cria snapshots de volumes EBS automaticamente** e **deleta snapshots expirados** com base em uma política de retenção configurável. Executa diariamente em todas as regiões via EventBridge.

---

## Como funciona

Uma única regra do EventBridge aciona o Lambda uma vez por dia. Em cada execução, a função:

1. **Cria** snapshots de todos os volumes EBS com a tag `ScheduledSnapshot = True`
2. **Deleta** snapshots mais antigos que `RETENTION_DAYS` criados anteriormente por este manager

```
EventBridge (cron diário) ──► Lambda
                                  │
                                  ├── describe_volumes (tag: ScheduledSnapshot=True)
                                  │       └──► create_snapshot  ──► adiciona tag ManagedBy
                                  │
                                  └── describe_snapshots (tag: ManagedBy=aws-ebs-snapshot-manager)
                                          └──► deleta se mais antigo que RETENTION_DAYS
```

> Somente snapshots criados por este manager (tag `ManagedBy = aws-ebs-snapshot-manager`) são deletados. Snapshots criados manualmente nunca são afetados.

---

## Funcionalidades

- Cria snapshots diários de todos os volumes tagueados em todas as regiões
- Deleta somente **snapshots gerenciados** — snapshots criados manualmente estão seguros
- **Política de retenção** configurável via variável de ambiente
- Copia a tag `Name` do volume para o snapshot para fácil identificação no console
- Suporte às ações `create`, `cleanup` ou `all` (padrão) de forma independente
- Filtragem server-side — somente volumes tagueados são retornados pela API
- Chamadas paginadas — funciona corretamente em qualquer escala
- Retry adaptativo — lida automaticamente com throttling da API da AWS
- Modo dry run — lista o que seria criado/deletado sem executar nada
- Logs JSON estruturados — compatíveis com queries do CloudWatch Insights

---

## Pré-requisitos

- Uma conta AWS
- Python 3.12+ (somente para desenvolvimento local)
- AWS CLI configurado (opcional, para deploy via linha de comando)

---

## 1. Adicionar tag nos volumes EBS

Adicione a seguinte tag em cada volume que deseja incluir no backup automático:

| Chave               | Valor  |
|---------------------|--------|
| `ScheduledSnapshot` | `True` |

> Volumes **sem** essa tag são completamente ignorados.

---

## 2. Criar o IAM execution role

**Policy document** — salve como `ebs-snapshot-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeRegions",
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots",
        "ec2:CreateSnapshot",
        "ec2:DeleteSnapshot",
        "ec2:CreateTags"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

**AWS CLI:**

```bash
aws iam create-role \
  --role-name ebs-snapshot-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam put-role-policy \
  --role-name ebs-snapshot-role \
  --policy-name ebs-snapshot-policy \
  --policy-document file://ebs-snapshot-policy.json
```

---

## 3. Fazer o deploy da função Lambda

### Opção A — Console da AWS

1. Acesse **Lambda → Criar função**
2. Nome: `ebs-snapshot-manager` | Runtime: **Python 3.12** | Arquitetura: `x86_64`
3. Execution role: selecione a role criada no passo 2
4. Faça upload do arquivo `lambda_function.py` (ou cole o código no editor inline)
5. Handler: `lambda_function.lambda_handler`
6. Timeout: **5 minutos** (varreduras multi-região levam tempo)
7. Salvar

### Opção B — AWS CLI

```bash
zip lambda_function.zip lambda_function.py

ROLE_ARN=$(aws iam get-role --role-name ebs-snapshot-role --query Role.Arn --output text)

aws lambda create-function \
  --function-name ebs-snapshot-manager \
  --runtime python3.12 \
  --role "$ROLE_ARN" \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://lambda_function.zip \
  --timeout 300

# Para atualizar após mudanças no código:
aws lambda update-function-code \
  --function-name ebs-snapshot-manager \
  --zip-file fileb://lambda_function.zip
```

---

## 4. Configurar a regra do EventBridge

Uma única regra diária cuida tanto da criação quanto da limpeza.

### Console da AWS

1. Acesse **EventBridge → Rules → Criar regra**
2. Selecione **Schedule** | Cron: `cron(0 2 * * ? *)` (executa às 02:00 UTC)
3. Target: **Lambda function** → `ebs-snapshot-manager`
4. Input: deixe como padrão — nenhum payload necessário

### AWS CLI

```bash
LAMBDA_ARN=$(aws lambda get-function --function-name ebs-snapshot-manager --query Configuration.FunctionArn --output text)

aws events put-rule \
  --name EBSSnapshotManager \
  --schedule-expression "cron(0 2 * * ? *)" \
  --state ENABLED

aws events put-targets \
  --rule EBSSnapshotManager \
  --targets "Id=1,Arn=$LAMBDA_ARN"

aws lambda add-permission \
  --function-name ebs-snapshot-manager \
  --statement-id AllowEventBridge \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn $(aws events describe-rule --name EBSSnapshotManager --query RuleArn --output text)
```

---

## Configuração

| Variável          | Padrão               | Descrição                                                        |
|-------------------|----------------------|------------------------------------------------------------------|
| `TAG_KEY`         | `ScheduledSnapshot`  | Chave da tag usada para identificar os volumes a serem snapshotados |
| `TAG_VALUE`       | `True`               | Valor esperado da tag (sensível a maiúsculas)                    |
| `RETENTION_DAYS`  | `7`                  | Número de dias para manter os snapshots gerenciados              |
| `DRY_RUN`         | `false`              | Defina como `true` para registrar ações sem executá-las          |

**AWS CLI:**

```bash
aws lambda update-function-configuration \
  --function-name ebs-snapshot-manager \
  --environment "Variables={TAG_KEY=ScheduledSnapshot,TAG_VALUE=True,RETENTION_DAYS=7,DRY_RUN=false}"
```

---

## Testes

### Dry run pelo console do Lambda

Acesse **Lambda → Testar** e use o payload abaixo para simular um ciclo completo sem criar ou deletar nada:

```json
{ "action": "all", "dry_run": true }
```

Para testar cada ação individualmente:

```json
{ "action": "create", "dry_run": true }
```

```json
{ "action": "cleanup", "dry_run": true }
```

### Dry run via AWS CLI

```bash
aws lambda invoke \
  --function-name ebs-snapshot-manager \
  --payload '{"action":"all","dry_run":true}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

---

## Exemplo de resposta

```json
{
  "action": "all",
  "dry_run": false,
  "retention_days": 7,
  "results": {
    "us-east-1": {
      "created": ["snap-0a1b2c3d4e5f6a7b8"],
      "deleted": ["snap-0z9y8x7w6v5u4t3s2"]
    },
    "sa-east-1": {
      "created": ["snap-0b2c3d4e5f6a7b8c9"],
      "deleted": []
    }
  }
}
```

---

## Tags dos snapshots gerenciados

Cada snapshot criado por este manager recebe as seguintes tags automaticamente:

| Tag                  | Valor                                   |
|----------------------|-----------------------------------------|
| `ManagedBy`          | `aws-ebs-snapshot-manager`              |
| `SourceVolumeId`     | ID do volume (ex: `vol-...`)            |
| `RetentionDays`      | Valor configurado de retenção           |
| `Name`               | `snapshot-<nome-do-volume>` (se o volume tiver tag Name) |

---

## Monitoramento

**Todos os snapshots criados hoje:**

```
fields @timestamp, region, volume_id, snapshot_id
| filter action = "create_snapshot" and not ispresent(error)
| sort @timestamp desc
```

**Todos os snapshots deletados hoje:**

```
fields @timestamp, region, snapshot_id, age_days
| filter action = "delete_snapshot" and not ispresent(error)
| sort @timestamp desc
```

**Erros:**

```
fields @timestamp, region, action, volume_id, snapshot_id, error
| filter ispresent(error)
| sort @timestamp desc
```

---

## Desenvolvimento local

```bash
pip install -r requirements-dev.txt

# Dry run local (requer credenciais AWS configuradas)
python -c "
from lambda_function import lambda_handler
result = lambda_handler({'action': 'all', 'dry_run': True}, None)
print(result)
"
```

---

## Contribuindo

Contribuições são bem-vindas! Por favor:

1. Faça um fork do repositório
2. Crie uma branch: `git checkout -b feature/sua-feature`
3. Faça commit das alterações: `git commit -m 'Adiciona sua feature'`
4. Faça push: `git push origin feature/sua-feature`
5. Abra um Pull Request

Mantenha os PRs focados — uma feature ou correção por PR.

---

## Licença

[MIT](LICENSE) — © Júlio César Santos
