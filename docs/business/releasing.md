# Releases e Bitbucket

## Gerar e verificar

Na raiz das fontes:

```sh
python3 -m unittest discover -s tools/distribution/tests
python3 tools/distribution/release.py plan
python3 tools/distribution/release.py build --apply
python3 tools/distribution/release.py verify --root outputs/releases/emulation-workspace-0.2.0-alpha.6
```

A allowlist `tools/distribution/config/release-files.json` relaciona origem e destino de cada arquivo. Configurações locais não entram: seus templates são gerados na instalação. Revisar mudanças na allowlist com o mesmo cuidado que mudanças de código. O pacote inclui um manifesto por arquivo e SHA-256 do tar.gz. O tarball usa ordem e timestamps fixos; mesma seleção de bytes/modos gera o mesmo pacote. Não sobrescrever release existente.

`verify` exige o conteúdo exato da release e deve rodar antes de inicializar a biblioteca; arquivos gerados pelo usuário depois da instalação são deliberadamente externos a esse manifesto. O manifesto acompanha o pacote: sua autenticidade depende do canal privado ou de assinatura futura. Ele detecta divergências, não substitui assinatura de distribuição.

## Repositório GitHub

O projeto usa o nome **EmuAtlas** e o repositório [Misael-art/EmuAtlas](https://github.com/Misael-art/EmuAtlas), branch `main`. A primeira publicação contém README, banner vetorial próprio e guias. O código e os arquivos de instalação ainda aguardam publicação revisada; não há release para download no GitHub nesta etapa.

O repositório foi criado público pelo responsável. Não confundir visibilidade do repositório com licença aberta ou aprovação comercial: os direitos por componente e a licença do código ainda exigem revisão. A configuração histórica da engenharia usa o identificador técnico `emulation-workspace` e a versão `0.2.0-alpha.6`; o nome de apresentação EmuAtlas não renomeia nem reescreve pacotes já gerados.

Para publicar código depois, exportar somente a allowlist revisada. Não inicializar Git sobre a biblioteca operacional nem enviar ROMs, BIOS, chaves, saves, userdata, executáveis de terceiros ou históricos locais. Autenticação fica no conector autorizado ou credential manager; nunca salvar token em remoto, arquivo ou log. Registrar commit, hashes e resultado da verificação no histórico local do projeto.

Nenhuma GitHub Action ou release foi criada nesta publicação documental. O pipeline Bitbucket existente nas fontes é configuração histórica e não comprova execução no GitHub. Uma futura integração contínua deve executar os testes isolados sem baixar biblioteca pessoal ou iniciar GUIs.
