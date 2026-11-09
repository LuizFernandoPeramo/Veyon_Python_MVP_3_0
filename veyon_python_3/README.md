# Veyon x Python — MVP 3.0

**Status:** protótipo de validação para uso local no Windows. Não foi testado no laboratório do usuário; só endpoints isolados foram previamente validados via PowerShell. Necessita autorização institucional antes de coletar dados reais.

## Pré-requisitos

- Python 3.11 ou superior e Veyon 4.11.2 no PC do professor.
- WebAPI do Veyon respondendo no loopback `http://localhost:11080/api/v1`.
- Chave `aluno/private` acessível **com autorização** à conta que executará o servidor.
- Acesso autorizado às estações Veyon e usuários informados sobre o monitoramento.

## Início

1. Descompacte em uma pasta protegida da máquina do professor.
2. Ajuste `config.json` com os computadores reais. O `host` é o nome da máquina na rede. Adicione estações explicitamente; o aplicativo **não** varre a rede.
3. Substitua os exemplos em `data/alunos.csv` pelo cadastro autorizado, nas colunas `cpf;nome;turma`, mantendo o arquivo fora de diretórios públicos. O cadastro **não** é enviado ao Veyon.
4. Abra PowerShell na pasta e execute `./iniciar.ps1` (ou `powershell -File .\iniciar.ps1` se permitido pela política existente). O script instala as bibliotecas em ambiente virtual e solicita uma senha de painel com 12+ caracteres.
5. Entre em `http://127.0.0.1:5050` no próprio computador. Acesse com a senha informada.
6. Selecione 1 estação para começar, marque a confirmação de autorização e clique **Iniciar monitoramento**. As capturas ocorrem imediatamente e depois a cada 5 minutos até solicitar parada. O painel se atualiza a cada 30 segundos.
7. Consulte **Histórico completo**. Para encerrar, clique **Solicitar parada**; aguarde a coleta em andamento finalizar e feche o console Python.

## Como funciona

- Autenticação: `POST /authentication/<HOST>` com `AuthKeys` em JSON; UID usado no cabeçalho `Connection-Uid`.
- Depois autentica, consulta `GET /session` até a sessão válida (até ~12 segundos), só então chama `GET /user` e `GET /framebuffer`.
- Armazena imagens JPEG sob `data/capturas` e índices em `data/historico.sqlite3`.
- Liga CPF do login Windows ao CSV; **não** exibe CPF completo nem o escreve no histórico. Logins não reconhecidos ficam como `Aluno nao cadastrado`.
- Cada coleta abre e fecha sua conexão; simplifica recuperação quando o Veyon expira a sessão. `HTTP 503` recebe até cinco tentativas na captura.
- Limpa registros e arquivos com mais de sete dias antes de cada rodada, conforme `retencao_dias`.

## Segurança e limitações

- O servidor Flask escuta **somente em `127.0.0.1`**. Não publique portas 5050 ou 11080 na rede. WebAPI HTTP só no loopback; para implantação remota, utilize TLS certificado e revisão da TI.
- A senha do painel não é gravada em arquivo, e a chave privada nunca sai do computador que executa o programa. Não compartilhe a chave.
- Restrinja permissões NTFS da pasta `data` aos professores/administradores aprovados. **O programa não configura ACLs NTFS automaticamente.**
- Não execute em computador compartilhado sem políticas de sessão, bloqueio de tela e proteção do perfil.
- A aplicação foi pensada para **um único processo Flask e um professor**. Não execute múltiplas instâncias nem use servidor de desenvolvimento em produção. Para implantação institucional, acrescentar autenticação central, controles por turma, auditoria e retenção formal.
- O `GET /user` pode trazer login vazio quando ainda não houver sessão Windows, ou não reconhecer aluno cujo login não corresponda a 11 dígitos do cadastro.
- As rotinas não incluem execução arbitrária de comandos, bloqueio ou desligamento de máquinas.
- O agendador roda no processo local: se o terminal fechar, o monitoramento para.
- Não há validação externa do aplicativo completo neste ambiente Windows. Realize testes controlados antes de escalonar.

Documentação: https://docs.veyon.io/en/latest/developer/webapi.html
