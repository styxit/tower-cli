# Copyright 2015, Ansible, Inc.
# Luke Sneeringer <lsneeringer@ansible.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import click

from sdict import adict

from tower_cli import models, get_resource, resources
from tower_cli.api import client
from tower_cli.utils import debug, exceptions as exc, types


class Resource(models.Resource, models.MonitorableResource):
    cli_help = 'Manage projects within Ansible Tower.'
    endpoint = '/projects/'

    name = models.Field(unique=True)
    description = models.Field(required=False, display=False)
    organization = models.Field(type=types.Related('organization'),
                                display=False, required=False)
    scm_type = models.Field(
        type=types.MappedChoice([
            ('', 'manual'),
            ('git', 'git'),
            ('hg', 'hg'),
            ('svn', 'svn'),
        ]),
    )
    scm_url = models.Field(required=False)
    local_path = models.Field(
        help_text='For manual projects, the server playbook directory name',
        required=False)
    scm_branch = models.Field(required=False, display=False)
    scm_credential = models.Field(
        'credential', display=False, required=False,
        type=types.Related('credential'),
    )
    scm_clean = models.Field(type=bool, required=False, display=False)
    scm_delete_on_update = models.Field(type=bool, required=False,
                                        display=False)
    scm_update_on_launch = models.Field(type=bool, required=False,
                                        display=False)

    @click.option('--monitor', is_flag=True, default=False,
                  help='If sent, immediately calls `project monitor` on the '
                       'project rather than exiting with a success.'
                       'It polls for status until the SCM is updated.')
    @click.option('--timeout', required=False, type=int,
                  help='If provided with --monitor, the SCM update'
                       ' will time out after the given number of seconds. '
                       'Does nothing if --monitor is not sent.')
    def create(self, organization=None, monitor=False, timeout=None,
               fail_on_found=False, force_on_exists=False,
               **kwargs):
        """Create a new item of resource, with or w/o org.
        This would be a shared class with user, but it needs the ability
        to monitor if the flag is set.
        """
        # First, run the create method, ignoring the organization given
        answer = super(Resource, self).write(
            create_on_missing=True,
            fail_on_found=fail_on_found, force_on_exists=force_on_exists,
            **kwargs
        )
        project_id = answer['id']

        # If an organization is given, associate it here
        if organization:

            # Get the organization from Tower, will lookup name if needed
            org_resource = get_resource('organization')
            org_data = org_resource.get(organization)
            org_pk = org_data['id']

            debug.log("associating the project with its organization",
                      header='details', nl=1)
            org_resource._assoc('projects', org_pk, project_id)

        # if the monitor flag is set, wait for the SCM to update
        if monitor:
            return self.monitor(project_id, timeout=timeout)

        return answer

    @resources.command(use_fields_as_options=(
        'name', 'description', 'scm_type', 'scm_url', 'local_path',
        'scm_branch', 'scm_credential', 'scm_clean', 'scm_delete_on_update',
        'scm_update_on_launch'
    ))
    def modify(self, pk=None, create_on_missing=False, **kwargs):
        """Modify an already existing.

        To edit the project's organizations, see help for organizations.

        Fields in the resource's `identity` tuple can be used in lieu of a
        primary key for a lookup; in such a case, only other fields are
        written.

        To modify unique fields, you must use the primary key for the lookup.
        """
        # Associated with issue #52, the organization can't be modified
        #    with the 'modify' command. This would create confusion about
        #    whether its flag is an identifier versus a field to modify.
        return super(Resource, self).write(
            pk, create_on_missing=create_on_missing,
            force_on_exists=True, **kwargs
        )

    @resources.command(use_fields_as_options=('name', 'organization'))
    @click.option('--monitor', is_flag=True, default=False,
                  help='If sent, immediately calls `job monitor` on the newly '
                       'launched job rather than exiting with a success.')
    @click.option('--timeout', required=False, type=int,
                  help='If provided with --monitor, this command (not the job)'
                       ' will time out after the given number of seconds. '
                       'Does nothing if --monitor is not sent.')
    def update(self, pk=None, create_on_missing=False, monitor=False,
               timeout=None, name=None, organization=None):
        """Trigger a project update job within Ansible Tower.
        Only meaningful on non-manual projects.
        """
        # First, get the appropriate project.
        # This should be uniquely identified at this point, and if not, then
        # we just want the error that `get` will throw to bubble up.
        project = self.get(pk, name=name, organization=organization)
        pk = project['id']

        # Determine whether this project is able to be updated.
        debug.log('Asking whether the project can be updated.',
                  header='details')
        result = client.get('/projects/%d/update/' % pk)
        if not result.json()['can_update']:
            raise exc.CannotStartJob('Cannot update project.')

        # Okay, this project can be updated, according to Tower.
        # Commence the update.
        debug.log('Updating the project.', header='details')
        result = client.post('/projects/%d/update/' % pk)

        # If we were told to monitor the project update's status, do so.
        if monitor:
            return self.monitor(pk, timeout=timeout)

        # Return the project update ID.
        return {
            'changed': True,
        }

    @resources.command
    @click.option('--detail', is_flag=True, default=False,
                  help='Print more detail.')
    def status(self, pk=None, detail=False, **kwargs):
        """Print the current job status."""
        # Get the job from Ansible Tower.
        debug.log('Asking for project update status.', header='details')
        project = client.get('/projects/%d/' % pk).json()

        # Determine the appropriate project update.
        if 'current_update' in project['related']:
            debug.log('A current update exists; retrieving it.',
                      header='details')
            job = client.get(project['related']['current_update'][7:]).json()
        elif project['related'].get('last_update', None):
            debug.log('No current update exists; retrieving the most '
                      'recent update.', header='details')
            job = client.get(project['related']['last_update'][7:]).json()
        else:
            raise exc.NotFound('No project updates exist.')

        # In most cases, we probably only want to know the status of the job
        # and the amount of time elapsed. However, if we were asked for
        # verbose information, provide it.
        if detail:
            return job

        # Print just the information we need.
        return adict({
            'elapsed': job['elapsed'],
            'failed': job['failed'],
            'status': job['status'],
        })
