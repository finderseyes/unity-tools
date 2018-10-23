import glob
import copy
import os
from subprocess import call

import yaml
import yamlordereddictloader
import xmltodict
from jinja2 import Template, Environment, meta, TemplateSyntaxError
from git.repo.base import Repo

from upkit import utils


def _normalize_uri(uri):
    details = ''
    sub_path = ''

    if '#' in uri:
        idx = uri.index('#')
        sub_path = uri[idx + 1:]
        uri = uri[:idx]

    if '@' in uri:
        prefixes = ('git@', )
        idx = len(uri)

        for prefix in prefixes:
            plen = len(prefix)
            if '@' in uri[plen:]:
                idx = uri.index('@', plen)
                break

        details = uri[idx + 1:]
        uri = uri[:idx]

    return uri, details, sub_path


class NugetResolver(object):
    scheme = 'nuget:'

    def __init__(self, package_linker):
        self.package_linker = package_linker

    def resolve(self, source):
        if not self.package_linker.package_folder:
            raise ValueError('"package_folder" is required but not specified, see -w parameter.')

        source = source[len(self.scheme):]
        package, version, sub_path = _normalize_uri(source)

        utils.mkdir_p(self.package_linker.package_folder)
        call('nuget install %s -Version %s -OutputDirectory "%s"' % (package, version,
                                                                     self.package_linker.package_folder),
             shell=True)
        return os.path.join(self.package_linker.package_folder, '%s.%s' % (package, version), sub_path)


class GitResolver(object):
    scheme = 'git:'

    def __init__(self, package_linker):
        self.package_linker = package_linker

    def resolve(self, source):
        if not self.package_linker.package_folder:
            raise ValueError('"package_folder" is required but not specified, see -w parameter.')
        
        repo_uri = source[len(self.scheme):]

        repo_uri, branch_or_tag, sub_path = _normalize_uri(repo_uri)

        # branch = branch_or_tag
        # tag = None
        # if branch_or_tag and ':' in branch_or_tag:
        #     idx = branch_or_tag.index(':')
        #     tag = branch_or_tag[idx + 1:]
        #     branch = branch_or_tag[:idx]

        repo_id = repo_uri
        if branch_or_tag:
            repo_id = '%s.%s' % (repo_uri, branch_or_tag)

        repo_id = repo_id.replace('.', '_').replace(':', '_').replace('/', '_')
        repo_path = os.path.join(self.package_linker.package_folder, repo_id)
        utils.mkdir_p(self.package_linker.package_folder)

        # Check if the repo already exists
        repo = None
        if os.path.isdir(repo_path):
            try:
                repo = Repo(repo_path)
                if not hasattr(repo.remotes, 'origin') or not repo_uri == repo.remotes.origin.url:
                    raise RuntimeError('Invalid existing repository %s' % repo_path)
                else:
                    repo.remotes.origin.pull()
            except:
                utils.rmdir(repo_path)
                repo = None

        # the repository does not exist.
        if not repo:
            repo = Repo.clone_from(repo_uri, repo_path)

        self._swith_branch_or_tag(repo, branch_or_tag)

        return os.path.join(repo_path, sub_path)

    def _swith_branch_or_tag(self, repo, branch_or_tag):
        if not branch_or_tag:
            return

        if hasattr(repo.remotes.origin.refs, branch_or_tag):
            branch = repo.remotes.origin.refs[branch_or_tag]
            branch.checkout()
        elif hasattr(repo.tags, branch_or_tag):
            tag_ref = repo.tags[branch_or_tag]
            tag_branch = repo.create_head(branch_or_tag, tag_ref.commit)
            tag_branch.checkout()
        else:
            raise ValueError('"%s" is not a valid branch or tag.' % branch_or_tag)


class PackageLinker(object):
    def __init__(self, config_file=None, package_folder=None, params={}):
        """
        :param config_file: the config file
        :param package_folder: the folder where Nuget and other remote packages will be resolved to.
        :param params: command-line parameters.
        """
        self.source_resolvers = [
            NugetResolver(self),
            GitResolver(self),
        ]

        self._jinja_environment = Environment()
        self._params = {
            '__cwd__': os.path.abspath(os.getcwd()),
        }

        if package_folder:
            self.package_folder = os.path.abspath(package_folder)
        else:
            self.package_folder = None

        if config_file:
            with open(config_file, 'r') as fh:
                content = fh.read()

                config_data = yaml.load(content, Loader=yamlordereddictloader.Loader)

                # parameters
                params_data = config_data.get('params', {})
                params.update({
                    '__cwd__': os.path.abspath(os.getcwd()),
                    '__dir__': os.path.abspath(os.path.dirname(config_file)),
                })

                self._params = copy.deepcopy(params)
                self._expand_params(params_data, exclude=params)

                # links
                links_data = config_data.get('links', {})

                def _to_link(i):
                    source = self._render_template(i.get('source'), self._params)
                    target_spec = i.get('target', None)
                    target = os.path.abspath(self._render_template(target_spec, self._params)) if target_spec else None
                    package_linkspec = i.get('linkspec', None)

                    return {
                        'source': source,
                        'target': target,
                        'linkspec': package_linkspec,
                    }

                self._links = [_to_link(item) for item in links_data]
        else:
            self._links = []
            # if params_config:
            #     self._params['__dir__'] = os.path.abspath(os.path.dirname(params_config))
            #     with open(params_config, 'r') as fh:
            #         content = fh.read()
            #         params_data = yaml.load(content, Loader=yamlordereddictloader.Loader)
            #         self._expand_params(params_data)
            #
            # # override params
            # self._params.update(params)
            #
            # # packages
            # if packages_config:
            #     if not packages_folder:
            #         raise ValueError('Missing parameter "packages_folder".')
            #
            #     self._params['__dir__'] = os.path.abspath(os.path.dirname(packages_config))
            #     with open(packages_config, 'r') as fh:
            #         content = fh.read()
            #         packages_data = xmltodict.parse(content)
            #
            #         def _to_link(i, pkg_folder, dest):
            #             name = '%s.%s' % (i.get('@id'), i.get('@version'))
            #             source = os.path.abspath(os.path.join(pkg_folder, name, 'content'))
            #
            #             return {
            #                 'name': name,
            #                 'source': source,
            #                 'destination': dest,
            #                 'linkspec': None,
            #             }
            #
            #         self._links = [_to_link(item, packages_folder, os.path.abspath(destination))
            #                        for item in utils.guaranteed_list(packages_data['packages']['package'])]

    def _try_resolve(self, source):
        normalized_source = source.strip()
        for resolver in self.source_resolvers:
            if not normalized_source.startswith(resolver.scheme):
                continue
            return resolver.resolve(normalized_source), resolver

        # fallback to file resolver.
        return utils.realpath(source), None

    def _expand_params(self, params_data, exclude={}):
        for k, item in params_data.items():
            if k not in exclude:
                self._params[k] = self._render_template(item, self._params)

    def _render_template(self, template, params={}):
        try:
            ast = self._jinja_environment.parse(template)
            variables = meta.find_undeclared_variables(ast)
            for v in variables:
                if v not in params:
                    raise ValueError('Unknown parameter "%s"' % v)
            return Template(template).render(**params)
        except TypeError as err:
            raise ValueError('Syntax error at "%s", error: %s' % (template, str(err)))
        except TemplateSyntaxError as err:
            raise ValueError('Syntax error at "%s", error: %s' % (template, str(err)))

    def run(self):
        for link in self._links:
            self.link(source=link['source'],
                      target=link['target'],
                      package_linkspec=link['linkspec'],
                      forced=True,
                      set_dir=('__dir__' in self._params),
                      params=self._params)

    def link(self, source=None, target=None, forced=False, package_linkspec=None, set_dir=True, params={}):
        """
        Link a source folder to a sub-folder in destination folder using given name.
        :param source:
        :param target:
        :param forced:
        :param package_linkspec:
        :param set_dir:
        :param params:
        :return:
        """
        if not source:
            raise ValueError('Missing required "source" parameter.')

        source, resolver = self._try_resolve(source)

        # utils.fs_link(source, target)
        linkspec_path = None
        if not package_linkspec:
            package_linkspec, linkspec_path = self.read_package_linkspec(source)

        # make a copy of the dict
        params = copy.deepcopy(params)
        if source:
            params['__source__'] = source
            if set_dir:
                params['__dir__'] = source if not linkspec_path else os.path.dirname(linkspec_path)

        if target:
            target = os.path.abspath(target)
            params['__target__'] = target

        # child packages
        child_packages = package_linkspec.get('links', None)
        if not child_packages:
            if not target:
                raise ValueError('Missing parameter "target" but no links can be found in the linkspec.')

            content = package_linkspec.get('content', None)
            if not content:
                utils.fs_link(source, target, hard_link=True, forced=forced)
            else:
                exclude = package_linkspec.get('exclude', None)
                exclude_items = set(
                    p for item in exclude
                    for p in glob.glob(os.path.abspath(self._render_template(item, params)))
                ) if exclude else set()

                content_items = [
                    p for item in content
                    for p in glob.glob(os.path.abspath(self._render_template(item, params)))
                ]
                for content_item in content_items:
                    if content_item in exclude_items:
                        continue

                    content_item_name = os.path.basename(content_item)
                    content_item_target = os.path.abspath(os.path.join(target, content_item_name))
                    utils.fs_link(content_item, content_item_target, hard_link=True, forced=forced)
        else:
            for item in child_packages:
                item_target = os.path.abspath(self._render_template(item['target'], params))

                content = item.get('content', None)

                # content will overwrite the source
                if not content:
                    item_source = os.path.abspath(self._render_template(item['source'], params))
                    utils.fs_link(item_source, item_target, hard_link=True, forced=forced)
                else:
                    exclude = item.get('exclude', None)
                    exclude_items = set(
                        p for i in exclude
                        for p in glob.glob(os.path.abspath(self._render_template(i, params)))
                    ) if exclude else set()

                    content_items = [p for i in content for p in
                                     glob.glob(os.path.abspath(self._render_template(i, params)))]
                    for content_item in content_items:
                        if content_item in exclude_items:
                            continue

                        content_item_name = os.path.basename(content_item)
                        content_item_target = os.path.abspath(os.path.join(item_target, content_item_name))
                        utils.fs_link(content_item, content_item_target, hard_link=True, forced=forced)

        # external packages
        external_packages = package_linkspec.get('external_links', None)
        if external_packages:
            for item in external_packages:
                item_source = os.path.abspath(self._render_template(item['source'], params))
                item_target = os.path.abspath(self._render_template(item['target'], params))
                utils.fs_link(item_source, item_target, hard_link=True, forced=forced)

                default_content = item.get('default_content', None)
                if default_content:
                    content_items = [
                        p for item in default_content for p in
                        glob.glob(os.path.abspath(self._render_template(item, params)))
                    ]
                    for content_item in content_items:
                        content_item_name = os.path.basename(content_item)
                        content_item_target = os.path.abspath(os.path.join(item_target, content_item_name))
                        if not os.path.exists(content_item_target):
                            utils.copy(content_item, content_item_target)

    def read_package_linkspec(self, source):
        """
        Reads the linkspec if exist in given source folder.
        :param source: the folder containing linkspec file
        :return: a linkspec dictionary or empty dictionary.
        """
        linkspec, path = self._read_linkspec_yaml_file(source)
        # linkspec = self._read_package_linkspec_file(source) if not linkspec else linkspec
        linkspec = {} if not linkspec else linkspec
        return linkspec, path

    def _read_linkspec_yaml_file(self, source):
        file = os.path.join(source, 'linkspec.yaml')
        if not os.path.isfile(file):
            file = os.path.join(source, 'linkspec.yml')

        if not os.path.isfile(file):
            file = os.path.join(source, 'content', 'linkspec.yaml')

        if not os.path.isfile(file):
            file = os.path.join(source, 'content', 'linkspec.yml')

        if not os.path.isfile(file):
            return None, None

        with open(file, 'r') as fh:
            content = fh.read()
            return yaml.load(content, Loader=yamlordereddictloader.Loader), file

    def _read_package_linkspec_file(self, source):
        file = os.path.join(source, 'package.linkspec')
        if not os.path.isfile(file):
            return None

        with open(file, 'r') as fh:
            content = fh.read()
            data = xmltodict.parse(content)
            transformed_data = {}

            #NOTE: transform to new schema here.
            link = data.get('link', None)
            if not link:
                raise ValueError('Missing <link> root.')

            if '@name' in link:
                transformed_data['name'] = link['@name']

            use_child_package_links = link.get('@useChildPackageLinks', '')
            if use_child_package_links in ['false', 'no', 'False', '']:
                use_child_package_links = False
            else:
                use_child_package_links = True

            if use_child_package_links:
                def _to_child_package(package_link):
                    return {
                        'source': package_link['@package'],
                        'target': '{{__default__}}/%s' % package_link['@package']
                    }

                child_package_links = link.get('childPackageLinks')
                if child_package_links:
                    transformed_data['child_packages'] = [
                        _to_child_package(l) for l in utils.guaranteed_list(child_package_links['link'])]

            external_package_links = link.get('externalPackageLinks', None)
            if external_package_links:
                def _to_external_package(package_link):
                    return {
                        'source': '{{%s}}' % package_link['@package'].replace('ref:', '').replace('.', '_'),
                        'target': package_link['@path'],
                    }

                transformed_data['external_packages'] = [
                    _to_external_package(l) for l in utils.guaranteed_list(external_package_links['link'])]

            return transformed_data

