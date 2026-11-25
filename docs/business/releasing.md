# Releases e Bitbucket

## Gerar e verificar

Na raiz das fontes:

```sh
python3 -m unittest discover -s tools/distribution/tests
python3 tools/distribution/release.py plan
python3 tools/distribution/release.py build --apply
python3 tools/distribution/release.py verify --root outputs/releases/emulation-workspace-0.2.0-alpha.7
```

A allowlist `tools/distribution/config/release-files.json` relaciona origem e destino de cada arquivo. Configurações locais não entram: seus templates são gerados na instalação. Revisar mudanças na allowlist com o mesmo cuidado que mudanças de código. O pacote inclui um manifesto por arquivo e SHA-256 do tar.gz. O tarball usa ordem e timestamps fixos; mesma seleção de bytes/modos gera o mesmo pacote. Não sobrescrever release existente.

`verify` exige o conteúdo exato da release e deve rodar antes de inicializar a biblioteca; arquivos gerados pelo usuário depois da instalação são deliberadamente externos a esse manifesto. O manifesto acompanha o pacote: sua autenticidade depende do canal privado ou de assinatura futura. Ele detecta divergências, não substitui assinatura de distribuição.

## Atualização de fontes em staging

O fluxo de atualização usa a seleção de fontes e templates de uma release extraída. `update-plan` confere o pacote de entrada, o manifesto instalado e todos os arquivos gerenciados. Fontes personalizadas bloqueiam a aplicação e preservam seu conteúdo. O plano simples permite revisar o escopo. `--prepare` cria propostas em `work/installation/updates/<id>/staging/` e preimagens, checkpoints e manifesto em `outputs/installation/updates/<id>/`. Esses diretórios guardam o trabalho temporário e o histórico da atualização de código.

```sh
python3 tools/distribution/release.py update-plan --incoming work/release-input
python3 tools/distribution/release.py update-plan --incoming work/release-input --prepare
python3 tools/distribution/release.py update-apply --operation-id source-update-ID --apply
python3 tools/distribution/release.py update-verify --operation-id source-update-ID
python3 tools/distribution/release.py update-rollback --operation-id source-update-ID --apply
```

Execute aplicação e reversão com a ferramenta revisada da release, usando `--root` para selecionar a instalação. Encerre frontends e runtimes normalmente antes da aplicação. O fluxo compartilha os locks da instalação, confere as precondições do lote inteiro e publica cada arquivo com o escritor durável existente. Fontes retiradas da seleção vão para `rollback/removed/`. Uma retomada aceita apenas preimagens ou propostas exatas. A reversão preserva fontes novas em `rollback/applied-files/` e recusa alterações posteriores. `update-verify` confere a seleção inteira, permitindo os dados adicionais da instalação.

O escopo abrange código, documentação e templates gerenciados. As configurações pessoais continuam nos fluxos especializados de configuração. Runtimes e núcleos usam os instaladores de componentes com seus recibos próprios. A matriz comercial exige ainda o ensaio de instalação e atualização no host contratado. O manifesto de distribuição depende da confiança no canal de aquisição e de sua revisão.

## Repositório GitHub

O projeto usa o nome **EmuAtlas** e o repositório [Misael-art/EmuAtlas](https://github.com/Misael-art/EmuAtlas), branch `main`. A publicação inicial contém README, banner vetorial próprio e guias. A atualização de portabilidade apresenta as fontes e os templates pela allowlist revisada em uma pull request; abrir a PR não incorpora a mudança à `main` nem publica executáveis ou uma release para download.

O repositório foi criado público pelo responsável. Não confundir visibilidade do repositório com licença aberta ou aprovação comercial: os direitos por componente e a licença do código ainda exigem revisão. A configuração histórica da engenharia usa o identificador técnico `emulation-workspace` e a versão `0.2.0-alpha.6`; o nome de apresentação EmuAtlas não renomeia nem reescreve pacotes já gerados.

Para publicar código depois, exportar somente a allowlist revisada. Não inicializar Git sobre a biblioteca operacional nem enviar ROMs, BIOS, chaves, saves, userdata, executáveis de terceiros ou históricos locais. Autenticação fica no conector autorizado ou credential manager; nunca salvar token em remoto, arquivo ou log. Registrar commit, hashes e resultado da verificação no histórico local do projeto.

A seleção `0.2.0-alpha.7` (05/11/2026) acrescenta o workflow `.github/workflows/ci.yml` (origem `tools/distribution/repository/github/ci.yml`): permissão `contents: read`, checkout sem credenciais persistidas, testes de distribuição e regressões do saneamento somente com fixtures, 7z e ImageMagick do runner; sem biblioteca pessoal, GUI, GPU, BIOS ou Steam. Um workflow presente não prova execução: confira os checks remotos reais. As releases `alpha.6` e anteriores permanecem imutáveis.

Antes de `alpha.7`, nenhuma GitHub Action ou release havia sido criada na publicação documental. O pipeline Bitbucket existente nas fontes é configuração histórica e não comprova execução no GitHub. Uma futura integração contínua deve executar os testes isolados sem baixar biblioteca pessoal ou iniciar GUIs.
